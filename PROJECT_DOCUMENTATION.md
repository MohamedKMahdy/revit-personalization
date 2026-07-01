# BIM Personalization — Comprehensive Project Documentation

**Thesis:** *Meta Learning for Realtime Behavioral Personalization* (Agent‑Augmented BIM Log Mining for Personalized Action Generation)
**Author:** Mohamed K. Mahdy · **Compiled:** 2026‑06‑30
**Sources:** the full git history and source of `revit-personalization` (127 commits, 2026‑05‑23 → 2026‑06‑30) and `bim-mcp`, cross‑read against the running system.

---

## Abstract

This document is the complete technical record of a Master's‑thesis system that **learns a user's repeated Revit workflow from event logs, understands it as a behavioral rule (not just a detected pattern), executes it in the user's live model through an agentic loop, learns from the user's corrections, and extends its own tool library** by turning ad‑hoc code the agent writes into permanent compiled Revit commands.

It is built from three cooperating repositories: **`revit-personalization`** (the Python "brain": detect → understand → resolve → execute → learn), **`bim-mcp`** (the student's own, locally‑buildable, self‑extending Revit MCP server that replaced an upstream fork), and **`generalBIMlog`** (the C# add‑in that logs user actions). The brain talks to the server over JSON‑RPC on `:8080`; the user talks to the brain through a chatbot on `:5000`.

The document has two parts. **Part I — Development history** reconstructs, from the commit history, what was built over the last month and why. **Part II — Architecture** documents each subsystem in depth, with file/line citations. Appendices give the operational facts (ports, paths, run/deploy commands), known limitations, the roadmap, and a glossary.

---

## System overview

```
Revit (user works) ──logs──► generalBIMlog (C# add-in) ──► .jsonl
   └─ detector (cluster repeats) ─► Pattern Agent (rich Motif) ─► Understanding + Memory (per-user prior)
        └─ resolve_routine_values (Mark etc., ANCHORED to the live model)
             └─ build_goal + run_executor (agentic, self-healing)
                  └─ JSON-RPC :8080 ─► bim-mcp plugin (in Revit) ─► places / sets / tags
        ◄─ learn_from_run (executions, last values, compiled skill, confirmed/corrected understanding)
   chatbot/chat_server.py (:5000, SSE UI) · predictor.py (proactive next action)
   SELF-EXTENSION: executor writes code ─► Tool Engineer compiles+hot-loads ─► new tool on :8080
```

The architecture figure is at **`docs/architecture.svg`** (vector, print‑ready).

The pipeline holds one spine end‑to‑end — **detect → understand → resolve values → execute → learn** — and every layer is kept deterministic and unit‑testable where possible (injectable `client`, `dispatch_fn`, `on_event`). The Python test suite stands at **212 tests**; `bim-mcp` builds with 0 errors for Revit 2025 and 2026.

---

## Table of contents

**Part I — Development history**
1. Development timeline — the Python brain
2. Development timeline — the owned MCP server

**Part II — Architecture**
3. Detection & learning (logger → detector → Pattern Agent → Motif)
4. Understanding & per‑user memory
5. The executor — agentic execution loop
6. Chatbot, predictor & the live surface
7. bim‑mcp — the owned Revit MCP server
8. Self‑extension — the grow / repair loop

**Appendices**
- A. Repositories & layout
- B. Operational facts — ports, paths, run & deploy commands
- C. Known issues & limitations
- D. Roadmap / what's left
- E. Glossary

---

# Part I — Development history

_This part narrates, from the commit history, what was built over the last month and why. It is the "what we did" record._

## Development timeline — the Python brain

This section narrates the evolution of the `revit-personalization` repository — the Python "brain" that mines a user's Revit logs, learns and understands their repeated workflows, and then executes those workflows in the live model through an agentic loop. The account is reconstructed from the full commit history (127 commits, `0a2411a` on 2026‑05‑23 → `1604b38` on 2026‑06‑30) cross‑read against the actual source. Each phase gives the problem it solved, what changed, the key files and functions, representative commit hashes, and the outcome.

Throughout, one architectural spine holds: **detect → understand → resolve values → execute → learn**, with every layer kept deterministic and unit‑testable where possible (an injectable `client`, `dispatch_fn`, and `on_event` mean the agentic loop runs in tests with neither the Anthropic API nor a live Revit). The test suite grew from a smoke harness to **212 tests** over the period.

---

### Phase 0 — Pipeline skeleton and the pivot to an owned execution path

The project began (`0a2411a`, "Initial commit: Agent‑Augmented BIM Log Mining system") as a log‑mining pipeline: a `mcp_server/log_reader.py`, an orchestrator scaffold, and `check_logs.py`. Early commits built out the orchestrator (`1514c73`) and a first `RoutineDetector`/`ShortcutRunner`/`NotificationUI` "CEI pipeline" (`773a56a`), aligned to the thesis methodology §4.1–4.4 (`c5f6729`).

The decisive early realization was that a read‑only Autodesk MCP could not *write* to Revit. Commit `a8c87fa` ("Correct execution architecture: C# add‑in IPC replaces read‑only Autodesk MCP") reframed execution around an in‑Revit write path over TCP on `:8080`. A long sequence of UI/plumbing commits (`0d6683a`…`13cc5ee`) built the chatbot, embedded it in Revit via a WebView2 dockable pane, and hardened the "Execute" button (nested‑transaction crash `016c4e5`, hosted‑family placement on the nearest wall `13cc5ee`). A security commit `3897900` introduced a **tool allowlist** (`shared/tool_allowlist.py`) so `send_code_to_revit` was unreachable by default.

Two refactors set the final shape: `4c26536` scoped the repo to "Python MCP + personalization" (the C# plugins moved to sibling repos), and `75e5097` sourced logs from the supervisor's **generalBIMlog** add‑in, retiring the bundled logger. By `97de17d`/`715cb61` execution had fully migrated to the (then) `mcp-servers-for-revit` contract, later replaced by the student's own `bim-mcp` server.

**Outcome:** a working end‑to‑end skeleton — logs in, a clustering detector, an LLM pattern agent, a chat UI, and a real TCP write path into Revit — onto which every later capability was layered.

### Phase 1 — The v0.2 similarity‑clustering detector

**Problem.** The v0.1 substring detector collapsed parameter/family routines, lost interleaved repeats, and mined arbitrary noisy substrings.

**What changed.** `ea84226` ("feat(detector): add v0.2 similarity‑clustering routine detector") introduced `detector/v2_cluster.py` with a deterministic, standard‑library pipeline documented in its module header: tokenize each record to a typed `{action}:{key}` token; **segment** by opening an `Instance` at each `Place` and attaching `SetParam` by `element_id` / `Tag` by `tagged_element_id` (closing on the next `Place` or an idle gap, which also bumps a session counter); featurize; and **greedily cluster** at threshold θ with a similarity of `w_set·Jaccard(featureset) + w_seq·(1 − normEdit(seq))`. Clusters of ≥ N members emit a `CandidateRoutine`. The commit also added `_common.py` (shared tokenizer), `synthetic.py`, and a v0.1‑vs‑v0.2 evaluation (`03107d6`), later a "possibility matrix" (`29ca4f5`) mapping the detector's operating envelope.

The `CandidateRoutine` schema (`shared/schemas.py`) deliberately carries **two independent axes** — `support` (frequency / cluster size) and `confidence` (grouping *tightness* for v0.2, frequency for v0.1) — with a class docstring warning they are not comparable across detector versions.

**Outcome:** param/family routines no longer collapse, interleaved repeats survive, and only `Place`‑rooted instances are mined. This detector became the stable substrate that the compound detector (Phase "1C") and predictor later reused verbatim.

### Phase 2 — The self‑healing agentic executor

**Problem.** The offline Macro Agent generated a fixed tool sequence *blind* to the live model, so it could not react to "no host wall" or "family not loaded".

**What changed.** `b9e6103` ("feat: self‑healing agentic executor") created `orchestrator/executor_agent.py` (now ~1,540 lines, the largest module in the brain). It runs the Anthropic tool‑use loop the module header describes:

```
while not done and iters < cap:
    resp = claude.create(tools=TOOLS, messages=...)
    if resp wants tools:  result = dispatch(tool, args); feed result back (is_error on failure)
    else:                 done
```

`run_executor()` streams every step via `on_event` so the user watches the self‑correction like a Claude Code transcript. A curated set of ergonomic tools (`place_element`, `set_parameter`, `tag_element`, plus grounding reads) is the allowlist; `real_dispatch()` maps each to a `revit_bridge` plugin call. `3e4e997` added `orchestrator/project_memory.py` ("the assistant remembers/understands the project"), and `eebb889` taught the executor to **query missing information before guessing** — the `EXECUTOR_SYSTEM` "LEARN FROM THE MODEL" block instructs it to call `get_available_family_types`/`inspect_model`/`get_selected_elements` rather than place blind.

`65e7ce3` added the **gated Revit‑API fallback** (`execute_revit_api`): a last‑resort C# snippet path that is transactional/undoable, streamed for oversight, and off by default for demos (`EXECUTOR_ALLOW_API_FALLBACK`). Crucially, a programmatic **`API_NUDGE`** brake (`guard_api_fallback`) redirects the *first* knee‑jerk escalation back to the structured tools — the agent must reaffirm to proceed — so a genuine capability gap still gets through but a reflexive drop‑to‑API does not. `74b5d22` stopped the agent dropping to the API on every structured‑tool failure; `724ead7` added a confirmation gate (`needs_confirmation`/`confirm_fn`) for any *writing* API fallback. `3aaeca3` exposed the **full plugin surface** (walls, floors, grids, rooms, dimensions, duplicate, delete, atomic groups, queries) to the executor, not just place/set/tag.

**Outcome:** an executor that recovers from live‑model failures, keeps the raw API as a gated last resort, and is fully injectable for testing.

### Phase 3 — Completion enforcement, sequential values, and cost control

**Problem.** Weaker models placed an element and declared victory without setting parameters or tagging; and "half‑done routines" turned out to stem from a **motif field‑name mismatch**.

**What changed.** `54728d9` ("Guarantee the whole routine runs: completion enforcement") computes the routine's required steps (`required_steps_from_motif`) and, if the model stops early, re‑prompts it (`_completion_nudge`, up to `MAX_COMPLETION_NUDGES`); if it still won't finish, it **completes the known steps deterministically** on the placed element. `PLACE_TOOLS`/`SETPARAM_TOOLS`/`TAG_TOOLS` sets recognize that e.g. `place_and_configure` both places *and* sets parameters. `252559a`… (`252559a`) fixed "the real cause of half‑done routines: motif field‑name mismatch" — reading the Pattern Agent's actual fields (`action_type`, `family_name`, …) with legacy fallbacks.

`3547415` ("Auto‑fill sequential parameter values") added `next_in_sequence()` — increment the last run of digits, preserving prefix/suffix and zero‑padding (`D-105`→`D-106`, `W-09`→`W-10`) — so a variable like `Mark` advances automatically instead of prompting.

Cost work landed in parallel: `2877f73` cached the tools+system prefix behind a `cache_control` breakpoint on the last tool (the ~19.8K‑token prefix serves at ~0.1× on repeat iterations) and logged per‑run spend; `85386f2` moved conversational chat replies to a cheaper model while pattern agents keep Opus; `5c4d6b4`/`dc88a2b` added a central provider switch (`shared/llm.py`) to flip any agent between Opus/Sonnet/Gemini; `50d71b6` survived Gemini free‑tier 429s with SDK backoff; `8058b7b` set a 1‑hour prompt‑cache TTL so the prefix survives a multi‑minute `pick_point`; and `9e4f56f` added a deterministic pre‑flight plus adaptive Haiku→Sonnet escalation. `1aaa24e` let the executor **learn from mistakes across runs** (a "WHAT WENT WRONG BEFORE" memory block is treated as authoritative on the first action of the next run).

**Outcome:** routines run to completion (place → set every parameter → tag), variable marks auto‑advance, and steady‑state placement runs cheaply or free.

### Phase 4 — Correct placement and honest transcripts

**Problem.** Placement silently created **zero** elements every time.

**What changed.** `8abd494` ("fix the REAL placement bug: resolve family name → FamilyTypeId") is the pivotal fix: the plugin's `create_point_based_element` matches by `typeId`/`category`, not by family name — so sending a name resolved nothing and "succeeded" creating 0 elements. `_resolve_type_id()` now resolves the family (+ optional type) to a loaded `FamilyTypeId` first. `1f04da5` fixed hosted‑family (door/window) placement and added full reviewable run transcripts (`executor_transcripts.jsonl`). `7137361` fixed `tag_element` for doors/windows by resolving the *tag* type id ourselves (`_resolve_tag_type_id`, `_TAG_CATEGORY`) — the plugin's auto‑find compares the tag's category to the element's, which never match.

**Outcome:** placement, parameter‑setting, and tagging actually persist in the live model, with an auditable transcript per run.

### Phase 5 — Richer‑workflow foundation: process‑acceleration harness + richer motif schema

**Problem.** The methodology promised process‑acceleration metrics but no harness existed, and the flat `Place→SetParam→Tag` motif could not express the compound/loop/conditional workflows people actually repeat.

**What changed.** `9eea8b4` ("richer workflows foundation") did three things at once:
- **`eval/process_acceleration.py`** — a deterministic, `$0` harness computing, per detected routine, MANUAL effort (actions, corrections = re‑edits of the same param, span_s) vs ASSISTED effort (`1` invoke + only the *free* variable params, since sequence‑resolvable variables like `Mark` cost nothing), then `actions_saved`/`reduction_pct`. Deliberately framed as a conservative, log‑derived lower bound, drop‑in upgradeable to an n=3–6 study via `--userstudy`.
- **Richer `Motif`/`MotifStep` schema** (`shared/schemas.py`) — optional fields that default to empty so flat motifs are unchanged: `element_role`, `host_role`, `condition`, `value_expr`, `repeat` (loop spec) on a step; `workflow_type` (`linear|compound|loop|conditional`) and `elements` on the motif.
- **A richer `build_goal()`** (`executor_agent.py`) that renders those extensions — a compound preamble naming each element by role, `_render_repeat()` for loops, `ONLY IF …` for conditions.

Then **Phase 1B** (`e8c9cc7`) taught the Pattern Agent (`orchestrator/pattern_agent.py`) to *propose* richer motifs, guarded by a deterministic **`_validate_and_downgrade()`** that strips any compound/loop/condition claim the recorded examples do not support (recording what it stripped in `_downgrade_notes`) — "a guarded failure is a publishable boundary finding, not a silently‑wrong automation." **Phase 1C** (`d74befe`) added **`detector/v3_compound.py`**: a `CompoundDetector` that assembles single‑element instances (including short ones v0.2 discards), then merges a later element into the running compound when its `Place.host_category` matches a category already present *and* it is temporally adjacent — the canonical "place a wall, then a door hosted on it, then tag the door" that v0.2 splits. It reuses v0.2's clustering unchanged, so it is a strict superset.

**Outcome:** measurable acceleration numbers for the results chapter, and the ability to *learn, guard, and detect* multi‑element routines — the boundary between what the evidence supports and what it does not is now explicit and testable.

### Phase 6 — Proactive prediction (realtime behavioural personalization)

**Problem.** The system was reactive — it waited for three repeats and then replayed. The thesis title claims *realtime*.

**What changed.** `bcb3d44` ("Phase 2: proactive next‑action predictor") added **`predictor.py`**. `current_prefix()` reads the in‑progress episode (from the most‑recent `Place` up to the next one) from the live eventlog; `NextActionPredictor.predict()` matches that prefix against the user's already‑learned routines — an **exact typed‑token prefix** first (highest support wins), falling back to an **action‑type prefix** at half confidence — and returns the remaining steps as a `Prediction` with a human `headline`. It is deterministic (~0 ms, no API), because prediction is about routines the user *already* repeats. `eefb118` surfaced it as a **suggestion chip** in the chat pane, and `eval/prediction_eval.py` measured precision@1.

**Outcome:** the assistant now offers the *next* step mid‑routine, not just a post‑hoc replay — the "realtime" leg of the thesis.

### Phase 7 — The conversational (free‑form) agent

**Problem.** The executor could only replay a *learned* routine; a user's arbitrary request ("how many fire doors on L2?", "renumber doors on L2 by room") had no path.

**What changed.** `546306b` ("Phase 3: free‑form conversational agent") added **`build_freeform_goal()`** (frames any NL request as an executor goal with a ground‑first, do‑only‑what‑was‑asked discipline) and a `/api/execute-task` SSE endpoint routed via a `##TASK:<nl>##` control token. Read‑only questions answer from the query tools without touching the model; writes still pass the confirm gate. Two follow‑ups closed real gaps found in live testing: `4f8c1f8` made free‑form tasks **conversation‑aware** (stop re‑doing completed work), and `1f01094` made the executor session **persistent** — `run_executor(prior_messages=…)` continues the *same* agent across tasks so it remembers element ids it created, families it found loaded, and what is already tagged (true cross‑task memory). **Phase 4** (`66855b5`) consolidated everything into `eval/ablations.py` — the deterministic `$0` before/after evidence (A1 acceleration, A2 compound recovery, A3 prediction precision@1, A4 richer‑goal coverage, A5 understanding‑vs‑detection).

**Outcome:** one chat surface handles learned‑routine replay, arbitrary tasks, and pure model questions, with genuine cross‑task memory.

### Phase 8 — Architecture steps 0/1/2/4: uniqueness, compiled skills, sequence induction, verifier

This cluster of commits turned "replay" from LLM re‑derivation into deterministic, verified automation.

| Commit | What it added | Key file/function |
|---|---|---|
| `c598ce5` | never assign a **duplicate Mark** — `resolve_routine_values` advances past values already in the live model | `executor_agent.resolve_routine_values` |
| `6c86ab7` | **understand the value sequence** — `induce_sequence_rule` infers prefix/suffix/zero‑pad + a constant *step* (needs ≥3 points / ≥2 agreeing diffs), not a hardcoded +1 (`W-100,W-105,W-110`→`W-115`) | `induce_sequence_rule`, `next_from_rule` |
| `5a16d21` | **compiled‑skill deterministic replay** — `orchestrator/compiled_skill.py`: `synthesize()` distills a successful run's tool_calls into a parameterized JSON program with holes (`{location}`, `{host_wall}`, `{<VariableParam>}`, `{eN}` for earlier‑created ids); `run_compiled()` replays it with no LLM, falling back to the agent on any unbindable step | `compiled_skill.synthesize/run_compiled` |
| `efa78d7` | **deterministic outcome verifier** — `verify_outcome()` reads the placed element's parameters *back* and confirms each value stuck (a "committed" result is not proof), also surfacing Revit's own warnings on the element | `executor_agent.verify_outcome` |

`compiled_skill.py`'s header states the honest novelty precisely: this is **motif‑guided distillation of one grounded demonstration into verified deterministic replay**, with a self‑healing agent as escalation — not program synthesis from scratch. Compiled replay is `$0`.

**Outcome:** a confirmed routine, after one successful agentic run, replays deterministically and free; every run is post‑condition‑verified against the live model; and no duplicate Mark is ever assigned.

### Phase 9 — Understanding Stages 0–4 (behavioural understanding, not just detection)

**Problem.** The thesis claims the system *understands* behaviour (rules + intent), not merely detects and replays. That claim needed a measurement harness, a deterministic core, confirmable hypotheses, and an audit trail.

**What changed**, stage by stage:
- **Stage 0** (`cb2c5a0`) — `eval/understanding_eval.py`: a **held‑out** harness that measures generalization (induce‑the‑rule vs literal replay) *before* building more.
- **Stage 1 core** (`3b1f118`) — **`orchestrator/rule_induction.py`**: `induce_conditional()` (numeric threshold or categorical map) and `induce_per_context_seq()` (an independent sequence per context field, e.g. Mark per level). The discipline is strict *honest abstention*: a conditional branch never demonstrated is **not invented**; a per‑context rule needs ≥2 identifiable groups; and every candidate must **reproduce every example with zero error** (`_reproduces`) or `induce_rule` returns `None`. A key overfit guard: a categorical conditional requires categories that **repeat** (`len(by_ctx) < len(pairs)`), so a per‑instance identifier like `Mark` can't "explain" another field.
- **Stage 1 live wiring** (`ca850b8`) — `resolve_routine_values` now applies the induced conditional / per‑level rule at runtime (via `_example_contexts` + `apply_rule`), so understanding actually drives execution.
- **Stage 2** (`c02928e`) — the Pattern Agent infers a motif **`intent`** (`goal`/`trigger`/`downstream`), shape‑checked by `_normalize_intent` and *never* auto‑applied (it is a hypothesis). The predictor carries the WHY/WHEN into its `headline`, hedged ("looks like…?").
- **Stage 3** (`d96e249`) — `orchestrator/understanding.py`: `describe_understanding()` renders each induced rule + intent as a plain‑language **confirmable hypothesis** (mixed‑initiative, Horvitz 1999); confirmation status lives in `project_memory.confirm_understanding`.
- **Stage 4** (`083b7c4`) — a reflection pass (cross‑routine prior), an **understanding ledger** (`understanding_ledger.jsonl`, appended by `log_understanding`) so understanding is auditable not asserted, and auto‑demotion of a repeatedly‑corrected rule (a `_rule_fingerprint` detects when a re‑induced rule materially changed and needs re‑confirmation).

Two hardening commits followed adversarial review: `efd31fe` (4 must‑fix + 5 polish across Stages 2–4), `6a55a4a` (a categorical‑conditional overfit caught by a fake‑log end‑to‑end test), and `7105023` (intent punctuation + pattern‑history isolation). `81215bc` extracted `level_name` into `ActionRecord` from the real generalBIMlog reader so per‑level induction works on real logs.

**Outcome:** the system induces *generating rules* (conditional / per‑context / sequence) that generalize to held‑out instances, states them as confirmable hypotheses, records intent, keeps an audit ledger, and abstains honestly when the evidence is insufficient — the defensible core of "behavioural understanding."

### Phase 10 — Reading Revit's own warnings + the outcome verifier tie‑in

**Problem.** A tool can report success while Revit still holds a **warning** against the element (duplicate Mark, unhosted, overlap) — the executor was blind to Revit's own failure messages.

**What changed.** `f3d3c74` ("read Revit warnings/errors/dialogs") added the **`get_warnings`** tool. It prefers the dedicated plugin command and falls back to a *fixed, audited* read‑only snippet (`GET_WARNINGS_CODE`, `transactionMode:'none'`) run through `send_code_to_revit` — so the model never authors the code and the call is confirmation‑exempt. The `EXECUTOR_SYSTEM` prompt gained a "READ REVIT'S WARNINGS" clause (warnings are *hints*, confirm with a read‑back; never silently ignore an Error‑severity warning on the element you just created), and `verify_outcome()` now escalates an Error‑severity warning naming the placed element to a hard issue.

**Outcome:** the self‑healing loop can react to Revit's own warnings and dialogs, not just tool return codes.

### Phase 11 — The most recent live‑assistant fixes

These commits (2026‑06‑28…06‑30) came directly from reading the user's live chat + executor transcripts, fixing real failure modes observed on a production model.

**Mark‑stuck one‑shot** (`d6d137b`). A chat‑typed value `##PARAM:Mark=101##` was stored as a *persistent* `rec["param_overrides"]` and applied *after* `resolve_routine_values` on every run, so every compiled replay pinned `Mark=101` and never incremented ("you made the mark 101, it should be 102 — same mistake again"). Fix: a chat‑typed value means "this placement," not "pin forever" — after a done run (`learn_from_run` has recorded it into `last_values`, so the sequence continues 101→102→103) the override is cleared and persisted.

**Anchor Mark to the live model + real tag offset** (`33ac8e5`). Root cause: `resolve_routine_values` computed the next Mark *only* from the routine's recorded examples (101,102,103 → 106); the live model's marks were read but used only for exact‑duplicate dedup, so when the project used a different scheme (148 doors `TU 29`…`TU 233`) the numeric `106` never collided and sailed through — ignoring the convention the user kept correcting. Fix: when the live model already has values for a variable param, **prefer that scheme** — induce a rule from the live marks if clean, else continue from the highest live mark (`TU 233`→`TU 234`). This closes the correction loop for free: a corrected Mark becomes a real element, so the next resolve reads it and continues (`TU 235`). The same commit fixed `tag_element`'s offset, which was hardcoded to `(0,0)` and silently overrode the bridge's 500 mm default — `offset_x`/`offset_y` are now in the schema and passed through, so "place the tag away from the door" is mechanically real. (`33ac8e5` also wired grown‑tool loading and the capture of every `execute_revit_api` use into a grow queue.)

**Per‑element read‑loop guard + batch rule** (`20bc979`). An "audit 60 doors" request looped `get_element_parameters` one door at a time, snowballing the conversation to ~605K input tokens (~$2) for a single request. A hard cost guard (`_READ_LOOP_TOOLS`, `_READ_LOOP_CAP=8`) short‑circuits after N per‑element reads with a nudge to batch, and the system prompt gained a "READ IN BULK, NEVER IN A LOOP" rule.

**`get_parameters_bulk`** (`b087761`). A new read‑only tool backed by a fixed, audited snippet (`_bulk_params_code`) that reads one or more parameters for **every** element of a category in a *single* call (the model only supplies a sanitized category + parameter names — it never authors code), returning `[{id, name, parameters}]`.

**Duplicate‑Mark bulk read** (`55bc2f4`). A live run set `Mark 'TU 88'` (a duplicate) because `_existing_param_values` (in `chat_server.py`) capped at 50 elements *and* looped per element — it sampled only the first 50 of 149 doors, so its max was ~87 and it collided with a door it never read. It now reads the whole category in **one** batched call (the same `_bulk_params_code` snippet), so the resolver sees the true max (234) and continues to `TU 235`.

**`place_element` honest success + family substitution** (`5b37656`). `create_point_based_element` can report success on a create that *rolled back* (a door/window with no valid host). Fix: after a reported success, `place_element` reads the element **back**; only a definitive not‑found blocks success (an empty/transient reply is trusted). And when the routine's family isn't loaded in *this* model, `_family_match()` substitutes the closest loaded family (scored by shared lowercased tokens) or lists the options and asks — no more thrash on a not‑loaded family, and no blind default that silently rolls back.

**Haiku‑first cost** (`3d8aad8`). `choose_start_model()` now starts a **simple** routine (place/set/tag/create) on Haiku — even its first run — not just memory‑warm ones, escalating to the Sonnet ceiling only after real difficulty (`ESCALATE_AFTER_FAILURES`). Only a cold *and* complex routine starts on the ceiling; adaptive is skipped when the ceiling is a free Gemini model (where paid Haiku would cost more).

**Outcome:** the live assistant now follows the project's real numbering convention (corrections carry forward through the live model), tags with the requested offset, never blows up cost on a bulk audit, never assigns a duplicate Mark, doesn't thrash on missing families, and runs simple placements on the cheapest capable model — with compiled replay at `$0`.

---

### Where the code lives (quick reference)

| Concern | File | Load‑bearing functions |
|---|---|---|
| Agentic loop, tool schemas, dispatch, value resolution | `orchestrator/executor_agent.py` | `run_executor`, `real_dispatch`, `resolve_routine_values`, `induce_sequence_rule`, `build_goal`, `verify_outcome`, `choose_start_model` |
| Deterministic replay | `orchestrator/compiled_skill.py` | `synthesize`, `run_compiled`, `can_replay` |
| Rule induction (conditional / per‑context) | `orchestrator/rule_induction.py` | `induce_conditional`, `induce_per_context_seq`, `induce_rule`, `apply_rule` |
| Confirmable understanding + ledger | `orchestrator/understanding.py` | `describe_understanding`, `log_understanding` |
| Motif extraction + downgrade guard | `orchestrator/pattern_agent.py` | `extract_motif`, `_validate_and_downgrade`, `_normalize_intent` |
| Detection | `detector/v2_cluster.py`, `detector/v3_compound.py` | `ClusterDetector`, `CompoundDetector.segment` |
| Proactive prediction | `predictor.py` | `current_prefix`, `NextActionPredictor.predict` |
| Chat surface / SSE / memory panel | `chatbot/chat_server.py` | `api_execute_smart`, `/api/execute-task`, `_existing_param_values` |
| Schemas | `shared/schemas.py` | `ActionRecord`, `Motif`, `MotifStep`, `CandidateRoutine` |
| Evaluation harnesses ($0, deterministic) | `eval/process_acceleration.py`, `eval/prediction_eval.py`, `eval/understanding_eval.py`, `eval/ablations.py` | `acceleration_for`, ablations A1–A5 |

The consistent engineering signature across all phases: **deterministic where possible, LLM only where necessary, and every inferred claim validated against the evidence** — the Pattern Agent's `_validate_and_downgrade`, rule induction's zero‑error refit, the outcome verifier's read‑back, the API‑fallback nudge, and the read‑loop / duplicate‑Mark cost guards are all expressions of the same discipline that makes the system defensible as thesis work rather than a demo.

## Development timeline — the owned MCP server

This section documents the engineering history of the Revit execution backend that the thesis relies on: the transition from a forked, CI-only server (`mcp-servers-for-revit`) to a fully owned, locally-buildable, self-extending server (`bim-mcp`). The narrative is reconstructed from the complete git history of both repositories (`C:/Users/DE1E7A/bim-mcp` and the retired fork `C:/Users/DE1E7A/mcp-servers-for-revit`) and from the source as it exists at each commit. Every commit hash, file path, and function name below was verified against the actual code.

The `bim-mcp` history is a single, tight, day-and-a-half sprint (28–30 June 2026) of sixteen commits that carry the server from an empty scaffold to a closed self-extension loop.

### 0. Starting point — why the fork could not build on this machine

The upstream project `mcp-servers-for-revit` (an MIT-licensed, Chinese-authored Revit MCP server by Duong Tran Quang / DTDucas) was forked and extended for the thesis (the fork's own history adds `get_warnings`, `pick_point`, a Test Tools WPF panel, TUM-branded ribbon icons, an auto-starting `:8080` socket server, and "10 new tools for BIM thesis tier 1–3 coverage"). The fork worked, but only through GitHub Actions — it could **not** be compiled on the thesis workstation.

The root cause is in the fork's project files. Both `plugin/RevitMCPPlugin.csproj` and `commandset/RevitMCPCommandSet.csproj` resolve their Revit API and SDK surface exclusively through NuGet packages that are blocked by this machine's SSL-proxied, restricted NuGet feed:

```xml
<!-- mcp-servers-for-revit/commandset/RevitMCPCommandSet.csproj -->
<PackageReference Include="RevitMCPSDK" Version="$(RevitMcpSdkVersion)" />
<PackageReference Include="Nice3point.Revit.Api.RevitAPI"   Version="$(RevitVersion).*" />
<PackageReference Include="Nice3point.Revit.Api.RevitAPIUI" Version="$(RevitVersion).*" />
<PackageReference Include="Nice3point.Revit.Toolkit"        Version="$(RevitVersion).*" />
<PackageReference Include="Nice3point.Revit.Build.Tasks"    Version="2.*" />
```

Two dependency families made a local `dotnet build` impossible:

| Dependency | Role in the fork | Why it blocked local build |
|---|---|---|
| `RevitMCPSDK` | Base classes (`ExternalEventCommandBase`), command registry, JSON-RPC wire types | NuGet-blocked; no local fallback |
| `Nice3point.Revit.*` | Revit API assemblies + implicit `global using` imports + build tasks | NuGet-blocked; the API refs and auto-usings vanish without it |

The fork's own CI file `.github/workflows/build-plugin.yml` makes the constraint explicit — it exists precisely to *work around* the local block: it builds "on a GitHub Windows runner (which has open nuget.org access)" and uploads the add-in as an artifact "for BOTH Revit 2025 and 2026 … without nuget.org on your own box."

This was fatal for the thesis's central ambition. A server that can only be rebuilt via a CI round-trip cannot support an agent that *codegens, compiles, and hot-loads a new tool in seconds*. The **grow loop** (an agent that grows and repairs its own tools during a session) is impractical if every new command requires a push, a runner, and an artifact download. The decision was therefore to build an **owned** server that removes the blocked dependencies entirely and compiles offline with one command.

### 1. Scaffold + vendored SDK — removing the blocked dependency surface

**Commit `81445fa`** *(scaffold bim-mcp: owned, locally-buildable, self-extending Revit MCP server)* laid the foundation: repo layout, MIT `LICENSE`, a `NOTICE` attributing the derivation to `mcp-servers-for-revit`, and the first slice of the **vendored SDK** under `src/Sdk/`. Rather than depend on `RevitMCPSDK`, the SDK is reimplemented as a handful of dependency-light `.cs` interfaces and POCOs: `IRevitCommand`, `IRevitCommandInitializable`, `ICommandRegistry`, `ILogger`, `IWaitableExternalEventHandler`, plus `CommandExecutionException` and `AIResult<T>` (the wire-result shape, copied verbatim so the Python contract in `revit_bridge.py` cannot drift).

**Commit `e97b9e7`** vendored the parts most likely to break the Python↔C# contract: the JSON-RPC wire models (`src/Sdk/JsonRpc/JsonRpcModels.cs` — `Request`/`SuccessResponse`/`ErrorResponse`/`ErrorCodes`, with `[JsonProperty]` names matched byte-for-byte to `revit_bridge.py`), the `RevitVersionAdapter`, the `RevitCommandRegistry`, and — the load-bearing piece — `ExternalEventCommandBase` (`src/Sdk/Base/ExternalEventCommandBase.cs`). This base class encodes the single most important correctness invariant of the whole server, the "Raise→wait handshake":

```csharp
// ExternalEvent created ONCE in the ctor (UI-thread context at startup)…
protected ExternalEventCommandBase(IWaitableExternalEventHandler handler, UIApplication uiApp)
{
    Handler = handler; UiApp = uiApp;
    _externalEvent = ExternalEvent.Create(handler);
}
// …raised per request from the socket thread, then block until the UI thread finishes.
protected bool RaiseAndWaitForCompletion(int timeoutMilliseconds)
{
    _externalEvent.Raise();
    return Handler.WaitForCompletion(timeoutMilliseconds);
}
```

- **Problem:** the fork's entire base-class + wire surface came from a blocked NuGet package.
- **Change:** re-implement that surface as a small owned assembly, with the JSON-RPC field names pinned to the existing Python bridge.
- **Key files:** `src/Sdk/Interfaces/*.cs`, `src/Sdk/Base/ExternalEventCommandBase.cs`, `src/Sdk/JsonRpc/JsonRpcModels.cs`, `src/CommandSet/Models/Common/AIResult.cs`.
- **Outcome:** the `RevitMCPSDK` dependency is fully replaced by owned source; the wire contract is preserved.

### 2. Proving a one-command local build (the milestone)

**Commit `8a6e7d4`** *(MILESTONE: owned MCP server builds locally (R2025 + R2026), 0 errors)* is the proof-of-concept for the entire strategy: a complete, minimal Revit MCP server that compiles with `dotnet build` on the workstation, with no CI and no blocked NuGet.

It established the **3-project structure** that the rest of the work builds on:

| Project | Responsibility |
|---|---|
| `BimMcp.Sdk` | Vendored interfaces / JSON-RPC / base classes. Its own assembly, so reflection-based command discovery sees a *single* `IRevitCommand` type (type identity matters for hot-loading later). |
| `BimMcp.Plugin` | The `IExternalApplication` host: `SocketService` (`:8080`), `CommandManager`, `RevitCommandRegistry`, `ConfigurationManager`, `Logger`, `PathManager`. Auto-starts on `ApplicationInitialized`. |
| `BimMcp.CommandSet` | The commands themselves (seeded here with `say_hello`), driven by `commandRegistry.json`. |

The dependency block was solved the way the sibling `RevitLogger`/`BIMAssistant` add-ins already build locally — the Revit API is referenced by **local DLL `HintPath`** with `Private=False`:

```xml
<Reference Include="RevitAPI">
  <HintPath>C:\Program Files\Autodesk\Revit $(RevitVersion)\RevitAPI.dll</HintPath>
  <Private>False</Private>
</Reference>
```

Multi-versioning uses `-p:RevitVersion=2025|2026`, targeting `net8.0-windows` (both Revit 2025 and 2026 run on .NET 8). The only remaining packages are *cached* ones (`Newtonsoft.Json`). The seed command `say_hello` returns success without touching the model — a clean build-plus-round-trip proof with no blocking dialog.

- **Outcome:** `dotnet build -c Release -p:RevitVersion=2026` (or `2025`) succeeds offline; the "owned, grows-as-we-use-it" plan is validated end-to-end at minimal scope.

**Commit `a242c65`** immediately exercised the add-a-command pattern by porting a second, read-only command, `get_warnings` (`Document.GetWarnings()` + recent modal pop-ups via a `DialogWatcher`), and added `scripts/build.ps1` (local build, both versions) and `scripts/deploy.ps1` (copies to `%APPDATA%\Autodesk\Revit\Addins\<ver>\`, refuses to run while Revit is open because the post-build copy locks the DLL).

### 3. The full 33-command port

**Commit `369fcfd`** *(port the FULL command surface from the fork (33 commands) — builds locally, both versions)* brought the owned server to feature parity with the fork in a single large change (`131 files changed, 18,027 insertions`). The ported surface covers the whole capability set: `ai_element_filter`, `analyze_model_statistics`, `color_splash`, the `create_*` family (`dimensions`, `grid`, `level`, `line`, `point`, `room`, `structural_framing`, `surface`), `delete_element`, `duplicate_element`, `execute_transaction_group`, `export_room_data`, `export_view_image`, the `get_*` reads, `operate_element`, `pick_point`, `place_and_configure`, `set_element_parameter`, `tag_element`, `tag_rooms`, and `tag_walls`.

The port was mechanical but non-trivial. Three moves made ~502 build errors go to 0:

1. **Namespace rewrite:** `RevitMCPSDK.*` → `BimMcp.Sdk`, `RevitMCPCommandSet` → `BimMcp.CommandSet`.
2. **`GlobalUsings.cs`** (`src/CommandSet/GlobalUsings.cs`) to replace the imports that `Nice3point.Revit.Build.Tasks` had auto-injected — this is what lets the bulk-ported files compile without editing each one:
   ```csharp
   global using Autodesk.Revit.DB;
   global using Autodesk.Revit.UI;
   global using Newtonsoft.Json;   global using Newtonsoft.Json.Linq;
   global using BimMcp.Sdk;         global using BimMcp.CommandSet.Models.Common;
   ```
3. **Cumulative `REVIT<ver>_OR_GREATER` symbols** defined per `RevitVersion` in `BimMcp.CommandSet.csproj`, so the ported code's `#if` guards select the modern APIs (`Floor.Create`, `Definition.GetDataType`) rather than the removed legacy ones (`NewFloor`, `ParameterType`).

At this point `commandRegistry.json` listed all 33 commands, temporarily on **port 8081** for a side-by-side bring-up (so the fork could stay live on `:8080` during verification). `send_code_to_revit` was deliberately deferred to the next step because it needs the Roslyn compiler DLLs vendored.

### 4. Ribbon + `send_code_to_revit` (vendored Roslyn) — replacement-ready

**Commit `6d291e4`** *(add ribbon UI + send_code_to_revit (vendored Roslyn) — full replacement-ready server)* closed the last gaps to full-replacement status:

- **Ribbon UI:** `src/Plugin/Core/Application.cs` now creates an *"MCP Server (bim-mcp)"* panel on the shared *"BIM Personalization"* tab, with a *"Revit MCP Switch"* button (`MCPServiceConnection`, an `IExternalCommand` toggling the socket). The server still auto-starts on `ApplicationInitialized`, but it is now *visible* in Revit like the fork.
- **`send_code_to_revit`:** ported `ExecuteCodeCommand` / `ExecuteCodeEventHandler` and **vendored the Roslyn compiler** — `Microsoft.CodeAnalysis.dll` and `Microsoft.CodeAnalysis.CSharp.dll` were committed into `libs/` (they are NuGet-blocked) and referenced by `HintPath`, deploying with the command set:
  ```xml
  <Reference Include="Microsoft.CodeAnalysis.CSharp">
    <HintPath>..\..\libs\Microsoft.CodeAnalysis.CSharp.dll</HintPath>
  </Reference>
  ```

`send_code_to_revit` is doubly important: it is the API fallback (the executor can run ad-hoc Revit code for a capability the compiled tools don't cover) *and* the grow loop's discovery engine (a captured fallback later becomes a compiled tool). This brought the total to **34 commands**, and `commandRegistry.json` was flipped to **port 8080** — the replacement port.

- **Outcome:** the owned server is now a drop-in replacement for the fork, built locally, both Revit versions, 0 errors.

### 5. The `:8080` cutover and fork retirement

**Commit `4770306`** added `scripts/replace.ps1`, the reversible cutover script. Run with Revit closed, it deploys `bim-mcp` (whose registry is already on `:8080`) and then **retires the fork by renaming its add-in manifest** rather than deleting it:

```powershell
Move-Item $forkAddin "$forkAddin.disabled" -Force   # mcp-servers-for-revit.addin -> .disabled
```

After this, only `bim-mcp` loads and owns `:8080`; the fork is dormant but recoverable. This is the formal handover point from the CI-only fork to the owned server.

### 6. De-sinicization + removing every blocking `TaskDialog`

**Commit `b6d5b65`** *(remove ALL Chinese text + ALL TaskDialog.Show popups from the ported command set)* addressed a correctness *and* presentation defect inherited from the bulk port. The fork is a Chinese-authored project, so the port carried Chinese comments/string literals and, more dangerously, modal `TaskDialog.Show(...)` error dialogs into the headless server.

The correctness problem: **a socket server must never pop a modal.** A modal blocks the UI thread, which is exactly the thread the command is waiting on — so the command times out with a Chinese dialog left open (this is what made `get_current_view_info` "time out"). The fix, applied in parallel across 40 files:

- Translated **every** Chinese character (comments + string literals) to English; verified 0 Chinese characters remain anywhere in `src/`.
- Removed **every** `TaskDialog.Show(...)` call from the command set (keeping the surrounding `catch`/result logic), leaving 0 in `CommandSet`.

- **Outcome:** the user sees no foreign-language text and receives no blocking dialogs from their own server; both versions build 0 errors.

**Commit `b21aafc`** restored the fork's command-management UX on the owned server: a *"Settings"* ribbon button opens `CommandSettingsWindow` (`src/Plugin/UI/CommandSettingsWindow.cs`), a code-only WPF window that enable/disables commands and writes `commandRegistry.json`; **commit `fe5fc0b`** then fixed `deploy.ps1` to copy *all* command-set DLLs (including the vendored Roslyn needed by `send_code_to_revit`), not just four named files.

### 7. `RevitDialogSuppressor` — truly headless

Removing `TaskDialog.Show` stopped the *server's own* popups, but live testing exposed a deeper gap: **Revit itself** raises modal dialogs and transaction "failures" the server never authored. **Commit `dd05e79`** *(add global RevitDialogSuppressor …)* addressed this. The prior `DialogWatcher` only *observed* dialogs; nothing dismissed them, and nothing handled transaction failures. So Revit's own modals ("Room is not in a properly enclosed region", "load classification cannot be empty") blocked the UI thread and timed out `create_room`, `create_structural_framing_system`, and `create_dimensions`.

`RevitDialogSuppressor` (`src/Plugin/Core/RevitDialogSuppressor.cs`, installed once at server start) subscribes two global handlers:

| Event | Handler action |
|---|---|
| `UIApplication.DialogBoxShowing` | `e.OverrideResult(1)` — auto-accept, so no modal is left open |
| `Application.FailuresProcessing` | `DeleteWarning` for warnings, `ResolveFailure` for resolvable errors, `ProceedWithRollBack` for the rest |

- **Outcome:** a command now either succeeds or fails fast with a message; it never hangs on a popup. This is what makes the server *truly* headless.

### 8. `send_code_to_revit` performance fix + `tag_element` fix

**Commit `da32355`** fixed two live-model bugs.

**`send_code_to_revit` performance.** The ported handler compiled user code by referencing **every** assembly loaded in the Revit process (`AppDomain.GetAssemblies()`). On large/federated models that is *hundreds* of add-in and link DLLs, and `MetadataReference.CreateFromFile` over all of them blew past the 60 s execution timeout — so *every* `send_code` call timed out, even `return "ok";`. The fix in `ExecuteCodeEventHandler.CompileAndExecuteCode` references only the .NET shared framework plus Revit and Newtonsoft:

```csharp
var tpa = AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") as string; // .NET shared framework
if (!string.IsNullOrEmpty(tpa))
    foreach (var p in tpa.Split(Path.PathSeparator)) AddRef(p);
AddRef(typeof(Autodesk.Revit.DB.Document).Assembly.Location);
AddRef(typeof(Autodesk.Revit.UI.UIApplication).Assembly.Location);
AddRef(typeof(Newtonsoft.Json.JsonConvert).Assembly.Location);
```
Compilation became fast and deterministic — which also unblocked the executor's `execute_revit_api` fallback on real models.

**`tag_element` auto-lookup.** The auto-select logic matched a tag family whose `FamilyCategoryId == the element category` — but a tag family's category is e.g. `OST_WallTags`, never `OST_Walls`, so it never matched ("No tag family found") even though `tag_walls` tagged the same walls fine. The fix in `TagElementEventHandler` (`src/CommandSet/Services/AnnotationComponents/TagElementEventHandler.cs`) maps each element category to its `*Tags` category and falls back to a Multi-Category tag:

```csharp
{ (long)BuiltInCategory.OST_Walls,   BuiltInCategory.OST_WallTags },
{ (long)BuiltInCategory.OST_Doors,   BuiltInCategory.OST_DoorTags },
// … then FirstOrDefault(fs => fs.Category.Id.Value == OST_MultiCategoryTags) as a fallback
```

### 9. Hot-load infrastructure — `Extensions/` + `reload_commands`

**Commit `b00b0c4`** *(self-extension infra: hot-load grown command DLLs …)* built the enabler for the grow loop: the ability to add a command to a **running** Revit with no restart. Each "grown" command is compiled to its *own* assembly referencing `BimMcp.Sdk`, dropped into `Commands\Extensions\`. New pieces:

- **`ExtensionLoader`** (`src/Plugin/Core/ExtensionLoader.cs`) — `Assembly.LoadFrom` each Extensions DLL, discovers `IRevitCommand` implementations, instantiates them **on the UI thread** (mandatory, because instantiation calls `ExternalEvent.Create`, which is UI-thread-only), and registers each by `CommandName`.
- **`reload_commands`** (`ReloadCommandsCommand` + `ReloadCommandsHandler`, `src/Plugin/Core/ReloadCommandsCommand.cs`) — runs the loader on the UI thread via an `ExternalEvent`, so a freshly compiled DLL hot-loads live and returns `{ success, loaded, total }`.
- `SocketService` loads `Extensions\` at startup and registers `reload_commands`; `PathManager.GetExtensionsDirectoryPath()` locates the folder.
- **`tools/grow/`** tooling: a template `GrownCommand.csproj`, `build_command.py` (compiles one `.cs` into a standalone DLL against the *deployed* `BimMcp.Sdk.dll` so reflection type identity holds), and a canonical `examples/get_levels.cs`.

Type identity is the subtle constraint that ties this together: because grown DLLs are compiled against the exact deployed `BimMcp.Sdk.dll` instance that Revit already loaded, the `IRevitCommand` seen by `ExtensionLoader` is the *same* type, so `typeof(IRevitCommand).IsAssignableFrom(type)` succeeds. This commit is the **last restart** — after it, growing a tool never requires reopening Revit.

### 10. The Tool Engineer workflow

**Commit `c8a0848`** *(Tool Engineer: grow-command workflow …)* added the self-extension/self-repair agent the project set out to build. `tools/grow/grow_command.workflow.js` is a multi-agent pipeline with an embedded SDK contract and the `get_levels` canonical example baked into the prompt:

| Phase | Agent role |
|---|---|
| **Write** | Generate the C# command source from a natural-language spec, following the vendored-SDK contract exactly. |
| **Review** | Check SDK conformance + safety (UI-thread handler, `_resetEvent.Set()` in `finally`, transactions wrap model changes, no dialogs); fix the file in place. |
| **Test** | `build_command.py` (compile → `Extensions` DLL) → `reload_commands` (hot-load) → call the new command live; `ok` only if build + load + `success=true`. |
| **Repair** | On failure, the Writer fixes from the build/test error; up to 3 rounds. |

`tools/grow/call.py` is the JSON-RPC helper the pipeline uses against `:8080`. The contract enforces the load-bearing pattern in prose (all Revit API access inside `Handler.Execute(UIApplication)`, `ManualResetEvent` discipline, transactions, "NO TaskDialog / MessageBox / Console — this is a headless socket server").

**Commit `44869e4`** *(grow loop PROVEN LIVE …)* is the first live proof. Run against the live GoldenNugget model on `:8080`, the Tool Engineer wrote, reviewed, compiled to standalone DLLs, hot-loaded (via `reload_commands`, reaching 38 commands), and live-tested two new commands with **zero Revit restarts**:

- `grown_command` — model statistics (live-verified: 42 levels, 74 grids, 10,744 elements);
- `room_area_by_level` — total placed-room area per level (live result: 35 rooms / 1994.59 m² across 3 levels).

### 11. Closing the grow loop — `set_view_scale`

**Commit `b219176`** *(grow loop closed end-to-end: promoted a captured code-fallback into a compiled tool (set_view_scale))* closed the loop that motivated the whole owned-server strategy. The full chain, proven end-to-end:

1. The executor hit a missing capability and wrote an ad-hoc `send_code_to_revit` fallback (`view.Scale = 50`).
2. A capture hook logged that fallback.
3. `promote_fallbacks.py` distilled it into a *parameterized* `set_view_scale` command (`tools/grow/grown/set_view_scale.cs`, reading `parameters["scale"]`), compiled and hot-loaded it, and live-tested it (set the active view's scale to 50).
4. Its tool schema was registered in `grown_tools.json`, so the executor now **advertises and dispatches `set_view_scale` as a first-class tool** instead of re-writing code.

- **Outcome:** the "by the time we're done we'll have all the tools we need" loop is demonstrated concretely — a real capability gap was observed, captured, crystallized into a compiled tool, and promoted into the executor's toolset, all without a CI round-trip or a Revit restart. This is the capability the CI-only fork could never have supported, and it is the payoff of the owned, locally-buildable design established back at commit `8a6e7d4`.

### Summary of the journey

| # | Commit | Milestone |
|---|---|---|
| 1 | `81445fa`, `e97b9e7` | Scaffold + vendored SDK (replaces blocked `RevitMCPSDK`) |
| 2 | `8a6e7d4` | Owned server builds locally, R2025 + R2026, 0 errors |
| 3 | `a242c65` | `get_warnings` + build/deploy scripts |
| 4 | `369fcfd` | Full 33-command port (502 → 0 errors) |
| 5 | `6d291e4` | Ribbon + `send_code_to_revit` (vendored Roslyn) → 34 commands |
| 6 | `4770306` | `:8080` cutover + fork retirement |
| 7 | `b6d5b65`, `b21aafc`, `fe5fc0b` | De-sinicization, no `TaskDialog`, Settings window, full-DLL deploy |
| 8 | `dd05e79` | `RevitDialogSuppressor` — truly headless |
| 9 | `da32355` | `send_code` perf fix + `tag_element` fix |
| 10 | `b00b0c4` | Hot-load infra: `Extensions/` + `reload_commands` |
| 11 | `c8a0848`, `44869e4` | Tool Engineer workflow; grow loop proven live |
| 12 | `b219176` | Grow loop closed: fallback → compiled `set_view_scale` tool |

The through-line is a single constraint driving a single decision: because the workstation's blocked NuGet feed made the fork buildable only via CI, and because CI round-trips are incompatible with an agent that grows its own tools in-session, the server was rebuilt as an owned, dependency-vendored, one-command-local-build project — and every subsequent commit (headless dialog handling, hot-loading, the Tool Engineer) exists to turn that local buildability into a working self-extension loop.

---

# Part II — Architecture

_This part documents each subsystem in depth, from the current source, with file/line citations._

## Detection & learning (logger → detector → Pattern Agent → Motif)

This section documents the offline detection-and-learning subsystem that turns raw Revit authoring logs into a generalised, reusable **Motif**. The pipeline is a strict left-to-right dataflow — *logger → adapter → detector → Pattern Agent → Motif* — in which every stage but the last is deterministic and standard-library only, and only the Pattern Agent invokes an LLM. The deterministic stages produce the evidence; the LLM proposes structure; and a final deterministic **downgrade guard** discards any proposed structure the evidence does not support. This design lets the thesis claim that any richer-workflow structure surfaced to the user is *evidence-backed*, not hallucinated.

### 1. The log source: generalBIMlog → `ActionRecord`

The system consumes logs written by the external **generalBIMlog `RevitLogger`** C# add-in, not a bespoke logger. That logger writes one `ProjectSchema` JSON file per project (`{projectGUID}.json`) under `%APPDATA%\Autodesk\Revit\Addins\<ver>\RevitLogger\Logs\eventlog\*.json`, in an **element-event, state-free** model: every event carries the element's *full* parameter snapshot rather than a delta. The pipeline's detector, however, expects a flat *action* stream keyed by element. The adapter that bridges the two is `mcp_server/generalbimlog_reader.py` (introduced in commit `75e5097`, "source logs from generalBIMlog RevitLogger; retire revit_addin plugin").

**Discovery and loading.** `eventlog_dirs()` (`generalbimlog_reader.py:53`) resolves the log directories: a `GENERALBIMLOG_DIR` override, else a glob over every installed Revit version's eventlog dir. `load_action_records()` (`generalbimlog_reader.py:260`) reads each JSON file with `utf-8-sig` (BOM-tolerant) and concatenates the converted records into one stream, skipping past any unreadable file rather than aborting.

**Snapshot-diffing conversion.** The core is `project_to_action_records(project)` (`generalbimlog_reader.py:187`). Because generalBIMlog is state-free, this function is *stateful*: it keeps a per-element `baseline` of the last-seen flattened instance parameters and diffs consecutive snapshots to recover `SetParam` actions. The event→action mapping is:

| generalBIMlog event | Emitted `ActionRecord` | Notes |
|---|---|---|
| `CREATED` (model element) | `Place` (op class `Model`) | seeds the param baseline; no `SetParam` |
| `CREATED` (annotation, `annotationKind == "Tag"`) | `Tag` (op class `Annotation`) | `tagged_element_id` from `taggedElementIds[0]` |
| `CREATED`/`REVISED` (Text/Dimension) | *skipped* | no slot in the Place/SetParam/Tag model |
| `REVISED` (model element) | one `SetParam` **per changed user-editable instance param** | snapshot diff vs. baseline |
| `DELETED` | `Delete` (detector ignores it in assembly) | drops the baseline entry |

**The "user-editable params only" filter.** A naïve diff would mistake Revit's internal recomputation (auto-join length/area, phase, IFC GUID, attach flags) for user edits. `_is_user_editable(key, storage_type)` (`generalbimlog_reader.py:106`) applies a three-list heuristic, explicitly documented as tunable against real logs: a `_DENY_KEYS` set plus `_DENY_SUFFIXES` (`_AREA`, `_VOLUME`, `_LENGTH`, …) always excluded; an `_ALLOW_KEYS` allow-list re-including meaningful `Double` numerics (wall height, sill/head height, width); otherwise inclusion by storage type (`_EDITABLE_STORAGE = {"String", "Integer", "ElementId"}`). Built-in param keys are mapped to friendly labels via `_FRIENDLY` (e.g. `ALL_MODEL_MARK → "Mark"`). The module docstring is candid that this is a documented limitation — "a snapshot diff cannot *prove* a change was user-initiated."

**`level_name` extraction (per-level induction).** A distinct helper, `_level_name(parameters)` (`generalbimlog_reader.py:148`), reads the element's level from whichever level-bearing built-in param is present — `_LEVEL_KEYS = (FAMILY_LEVEL_PARAM, WALL_BASE_CONSTRAINT, SCHEDULE_LEVEL_PARAM, INSTANCE_REFERENCE_LEVEL_PARAM, …, ROOM_LEVEL_ID)` — returning the param's readable `ValueString` (e.g. `"L1"`). This is set on *every* record in `common` (`generalbimlog_reader.py:209`). Commit `81215bc` ("extract element level into ActionRecord.level_name") explains the rationale precisely: the level must come from the *historical log*, because a live `element_id → level` query cannot recover the level of an element placed in a past session — which is exactly what learning a per-level convention (e.g. "Mark restarts per floor") requires. This closed a gap a fake-log test had flagged: without it, the per-level rule layer could never fire on real data.

**`ActionRecord` schema.** The shared log unit is `shared/schemas.py::ActionRecord` (`schemas.py:20`). Its field names are snake_case to match the JSON keys the C# logger emits, and the docstring cites Jang & Lee (2023, arXiv:2305.18032) enhanced BIM logging. Notable fields, grouped:

```python
# identity / grouping
schema_version="2.0"; event_id; session_id; transaction_id; transaction_name
# action taxonomy (Jang et al. 2023 AEI lexicon)
action_type: Literal["Place","SetParam","Tag","Delete"] = "Place"
operation_class: Literal["Model","Parameter","Annotation","View"] = "Model"
# element context
element_id; element_category; family_name; type_name; level_name; phase_name; host_category
# SetParam diff (reproducibility, per Jang §4.2)
param_name; param_storage_type; param_value_before; param_value_after
# Tag
tag_family_name; tagged_element_id
```

`class Config: extra = "ignore"` (`schemas.py:83`) lets future schema versions add fields without breaking the reader. Convenience properties `.action` and `.timestamp` alias `action_type`/`timestamp_unix` for the detector code (`schemas.py:72`).

### 2. The detector — `v2_cluster.py` (cluster-of-repeats) and `v3_compound.py` (multi-element)

The detector's job is *unsupervised*: given the flat `ActionRecord` stream, group repeated work into `CandidateRoutine`s with recorded examples, without any labels. Two versions form a superset relationship; `v2` is the default and `v3` extends it (`detector/__init__.py`, `make_detector`; `mcp_server/log_reader.py::_resolve_detector` — "v0.2 is the default; v0.1 … explicit; v3 selected by name/alias"). v0.1 (`SubstringDetector`) is retained only as a precision/recall baseline.

#### v0.2 — `ClusterDetector` (cluster-of-repeats)

`detector/v2_cluster.py` (commit `ea84226`) is a six-stage deterministic pipeline (docstring, `v2_cluster.py:4`):

1. **Tokenize** — each record becomes a typed token `"{action_type}:{key}"` via `_common.token()` (`_common.py:34`), where `derive_key()` (`_common.py:17`) is `family_name` for Place, `param_name` for SetParam, `tag_family_name` for Tag. Carrying the *key* in the token is the fix for the v0.1 weakness where param/family routines collapsed together.
2. **Segment** — `segment()` (`v2_cluster.py:92`) walks the time-sorted stream, opening a new `Instance` at each `Place` and attaching subsequent `SetParam` (by `element_id`) and `Tag` (by `tagged_element_id`) to that open instance. An **idle gap** larger than `idle_gap_minutes` (default 5) closes all open instances and increments a session counter, so gap-separated work splits into separate sessions. Only `Place`-rooted instances with `≥ min_instance_tokens` (default 3) survive. Segmenting by id rather than position is what lets *interleaved* repeats survive.
3. **Featurize** — each `Instance` (`v2_cluster.py:46`) exposes an ordered `tokens` list and a flat `feature_set` frozenset (`{fam:…, param:…, tag:…}`).
4. **Cluster** — greedy average-linkage grouping (`cluster()`, `v2_cluster.py:142`) at threshold `theta` (default 0.80). Pairwise `similarity()` (`v2_cluster.py:137`) blends set and sequence signals:

   ```
   similarity(a,b) = w_set · Jaccard(featureset_a, featureset_b)      # default w_set = 0.6
                   + w_seq · (1 − normalizedEditDistance(tokens_a, tokens_b))   # default w_seq = 0.4
   ```

   Edit distance (Levenshtein) plus Jaccard means minor variation still groups instead of forcing exact equality. The clustering is explicitly documented as deterministic: instances are consumed in `(start_time, element_id)` order and the strict `>` tie-break keeps the earliest-created cluster, so identical input always yields identical clusters (`v2_cluster.py:143`).
5. **Threshold** — in `detect()` (`v2_cluster.py:231`), only clusters with `≥ min_cluster_size` members (default 3) emit a `CandidateRoutine` via `_to_candidate()` (`v2_cluster.py:205`).
6. **Cooldown** — a structural signature surfaced within `cooldown_minutes` (default 10, by *data* time) is suppressed; the existing cluster is grown in the `_store` ledger instead (`v2_cluster.py:250`). `active_candidates()` still returns cooldown-grown clusters for inspection.

The emitted `CandidateRoutine` (`schemas.py:95`) carries two independent ranking axes, and the docstring is careful that they are not comparable across versions:

| axis | meaning in v0.2 | source |
|---|---|---|
| `support` / `count` | cluster size (frequency signal) | `len(members)` |
| `confidence` | *tightness* = mean pairwise intra-cluster similarity (0–1) | `_mean_pairwise_similarity()` (`v2_cluster.py:180`) |

The candidate's representative example (`medoid`, `v2_cluster.py:193`) drives the label (`build_label` → `"Place(M_Single-Flush) → SetParam×4 → Tag(Door Tag)"`), the compact `action_signature` (`"P,S,S,S,S,T"`), and the routine `id` (`routine_id_from_signature`), all from `_common.py` and kept ID-compatible with the C# `RoutineDetector`.

`DetectorConfig` (`detector/base.py:15`) is a frozen dataclass holding these knobs; both detectors implement the same `Detector` protocol (`base.py:39`) so they are interchangeable in the harness.

#### v0.3 — `CompoundDetector` (multi-element)

`detector/v3_compound.py` (commit `d74befe`) subclasses `ClusterDetector` to catch the canonical case v0.2 splits: "place a wall, then a door **hosted on it**, then tag the door." v0.2 segments one element per instance, so this compound is lost. v0.3 overrides only `segment()` (`v3_compound.py:74`) and reuses v0.2's clustering unchanged, making it a strict superset:

- `_assemble_singles()` (`v3_compound.py:32`) re-runs v0.2-style segmentation **without** the `min_instance_tokens` filter, so short single-element instances (a bare wall `Place`) survive to be merged.
- The merge rule is host-linked: a later single joins the running compound only if it is **temporally adjacent** (same session, within the idle gap) **and** its `Place.host_category` matches a category already in the compound (`v3_compound.py:83`). An unhosted element, or one hosted on something not in the run, starts a fresh compound.

This host-link requirement is the explicit bound on over-merging; the hard tail (elements with no host metadata) is left to optional future LLM segmentation. Merged compounds are then clustered by the inherited v0.2 logic, so flat single-element routines still detect exactly as before.

### 3. The Pattern Agent — learning a rich Motif

`orchestrator/pattern_agent.py` (`extract_motif`, `pattern_agent.py:183`; commit `e8c9cc7` "Pattern Agent learns richer motifs, with a deterministic downgrade guard") takes the *k* recorded examples of one `CandidateRoutine` and generalises them into a `Motif`. It runs on `claude-opus-4-8` with adaptive extended thinking (`pattern_agent.py:19, :227`). It is invoked from `orchestrator/agents.py:139` (step 2/5 of the orchestrator) and from `pattern_watcher.py:104`, each passing `RoutineExample.model_dump()` dicts.

**Input slimming.** Before the call, each action dict is reduced to a `KEEP_FIELDS` whitelist (`pattern_agent.py:199`) — action type, family/type/category, param name and before/after values, tag family, and crucially `level_name`, `view_type`, `transaction_name` — and empty/zero values are dropped, so the prompt carries only decision-relevant fields.

**System prompt (the invariant-vs-variable rule).** The prompt (`pattern_agent.py:22`) instructs the model, per Jang & Lee §4.2, to distinguish constant from variable parameters: a `SetParam` whose `param_value_after` is identical across all examples becomes `param_value_type="constant"` with the literal value; one that differs becomes `"variable"`, `param_value=null`, and its `param_name` is added to `parameters_to_prompt` (the params the user must supply at replay). It also permits, *only when the examples demand it*, the richer-workflow structures (compound/loop/conditional/computed-value) and an inferred `intent`, with the explicit warning that "a downstream validator will strip any claim the examples do not support."

**Output validation.** The returned text is fence-stripped and JSON-parsed (`pattern_agent.py:242`), then checked for the required keys `{name, description, steps, preconditions, parameters_to_prompt}` (`pattern_agent.py:255`) before the two deterministic post-processors run.

### 4. The Motif / MotifStep schema

The output type is `shared/schemas.py::Motif` (`schemas.py:151`), a generalised routine composed of `MotifStep`s (`schemas.py:121`). The schema was deliberately designed as a **backward-compatible extension**: the base fields describe a flat single-element step (the original shape, still the default), and every richer field defaults to empty so existing flat motifs are unchanged (commit `9eea8b4`, "richer workflows foundation").

`MotifStep` fields:

| field | applies to | meaning |
|---|---|---|
| `action_type` | all | `Place` / `SetParam` / `Tag` |
| `family_name` | Place | Revit family placed |
| `param_name`, `param_value`, `param_value_type` | SetParam | value + `"constant"` vs `"variable"` (`null` value = prompt user) |
| `tag_family_name` | Tag | tag family |
| `element_role` | multi-element | *which* element of a compound this step acts on (e.g. `"door"`), so later steps can refer back to it |
| `host_role` | hosted Place | role of the element to host on (e.g. door `host_role="wall"`) |
| `condition` | SetParam | a guard, e.g. `"width>1500"` — the step runs only when it holds |
| `value_expr` | SetParam | a *computed* value instead of a literal, e.g. `"2*height"` or `"room.number"`, evaluated against the live model |
| `repeat` | Place | a loop spec: `{"over":"selected_walls","spacing_mm":2000,"index_param":"Mark","mark_expr":"D-{i:02}"}` or `{"count":5}` |

`Motif` fields add two pieces of richer-workflow metadata plus intent:

- `workflow_type` (`schemas.py:168`): `"linear" | "compound" | "loop" | "conditional"` — tells the runtime how to read the steps. Default `"linear"`.
- `elements` (`schemas.py:169`): the distinct elements of a compound routine and their host relationships, e.g. `[{"role":"wall","family":"Basic Wall"},{"role":"door","family":"M_Door...","host":"wall"}]`.
- `intent` (`schemas.py:174`, commit `c02928e`): the inferred *latent* — a `{goal, trigger, downstream}` hypothesis of *why* and *when* the routine runs, e.g. `{"goal":"a schedule-ready tagged door","trigger":"a door placed with no Mark","downstream":"the door schedule"}`.

Example flat vs. richer motif shape:

```json
{ "name": "Place Door + Mark + Tag", "workflow_type": "linear",
  "steps": [
    {"action_type":"Place","family_name":"M_Single-Flush"},
    {"action_type":"SetParam","param_name":"Mark","param_value":null,"param_value_type":"variable"},
    {"action_type":"Tag","tag_family_name":"Door Tag"}],
  "parameters_to_prompt": ["Mark"] }
```

### 5. The downgrade guard (a claimed loop/condition must reproduce the examples)

The safety net is `_validate_and_downgrade(motif, examples)` (`pattern_agent.py:112`) — the deterministic gate that keeps a richer-motif claim **only when the recorded examples actually support it**, falling back to a flat motif otherwise. Its stated purpose is to prevent over-generalisation (a loop/compound/condition hallucinated from flat single-element examples), and its design philosophy is explicit in the docstring: *"a guarded failure is a publishable boundary finding, not a silently-wrong automation."* It records exactly what it stripped in a `_downgrade_notes` list for transparency.

The guard first recomputes the ground-truth support signals directly from the examples (`pattern_agent.py:118`):

```python
supports_compound = any(len(set(f)) >= 2 for f in place_fams)   # ≥2 DISTINCT families placed
supports_loop     = any(max((f.count(x) for x in set(f)), default=0) >= 2  # SAME family placed ≥2×
                        for f in place_fams)
```

It then reconciles the LLM's claims against these signals:

| claimed structure | kept only if | else |
|---|---|---|
| `workflow_type == "compound"` / non-empty `elements` | `supports_compound` | → `"linear"`, `elements=[]`, note |
| `workflow_type == "loop"` / a step's `repeat` | `supports_loop` | → `"linear"`, `repeat` stripped, note |
| step `element_role` / `host_role` | `supports_compound` | popped |
| step `condition` / `value_expr` on param `pn` | that param takes **>1 distinct value** across examples (`len(param_values[pn]) > 1`) | popped, note |

The condition/value_expr rule is the crux of "a claimed condition must reproduce the examples": a guard like `"width>1500"` or a computed `value_expr` is only meaningful if the parameter *actually varies* across the recorded runs; on a constant param it is stripped as unsupported speculation. This behaviour is pinned by `tests/test_pattern_agent_guard.py` — e.g. `test_loop_downgraded_when_examples_show_single_placement` (a single `M_Door` per example → loop downgraded to linear, `repeat` removed), `test_loop_kept_when_examples_repeat_the_family` (three `M_Door` placements in one example → loop kept with `spacing_mm`), `test_condition_stripped_on_constant_param` vs. `test_condition_kept_on_varying_param`, and `test_flat_motif_untouched`.

**Intent is treated differently.** `_normalize_intent(motif)` (`pattern_agent.py:167`) does *not* validate intent against the examples — because intent is an inferred latent (the WHY/WHEN) meant to be **confirmed with the user** (Understanding Stage 3), never auto-applied. It only shape-checks it into `{goal, trigger, downstream}` strings and drops it if neither `goal` nor `trigger` survives. This asymmetry — richer *structure* must reproduce the evidence, but the *hypothesis of intent* is merely shape-checked and surfaced for confirmation — is the central epistemic contract of the learning stage.

---

**Key file references.** Log adapter: `mcp_server/generalbimlog_reader.py` (`project_to_action_records:187`, `_level_name:148`, `_is_user_editable:106`). Schemas: `shared/schemas.py` (`ActionRecord:20`, `MotifStep:121`, `Motif:151`, `CandidateRoutine:95`). Detectors: `detector/v2_cluster.py` (`ClusterDetector`, `segment:92`, `similarity:137`, `detect:231`), `detector/v3_compound.py` (`CompoundDetector.segment:74`), helpers in `detector/_common.py`, config in `detector/base.py`. Pattern Agent: `orchestrator/pattern_agent.py` (`extract_motif:183`, `_validate_and_downgrade:112`, `_normalize_intent:167`). Wiring: `mcp_server/log_reader.py` (`_resolve_detector:128`, detector loop `:191`), `orchestrator/agents.py:139`. Guard tests: `tests/test_pattern_agent_guard.py`. Relevant commits: `75e5097` (generalBIMlog source), `81215bc` (`level_name`), `ea84226` (v0.2), `d74befe` (v0.3), `9eea8b4` (richer-motif schema foundation), `e8c9cc7` (richer Pattern Agent + downgrade guard), `c02928e` (intent).

## Understanding & per-user memory

This subsystem is the "personalization brain" of the thesis prototype: it turns the raw examples the user recorded into *confirmable, self-improving understanding* of the user's conventions, persists that understanding per user, and closes the loop so that a correction the user makes is genuinely honoured on the next run rather than lost to LLM prose. It spans three deterministic, dependency-free, `$0` modules — `orchestrator/rule_induction.py` (Stage 1: what generated this value?), `orchestrator/understanding.py` (Stages 3–4: render as hypotheses + ledger), and `orchestrator/project_memory.py` (persistent per-user store, active confirmation, reflection) — plus the runtime resolver `executor_agent.resolve_routine_values` that actually *applies* the learned scheme. The design lineage is explicit in the code: OpenClaw-style `CLAUDE.md`/`MEMORY.md` file-based memory rather than a vector-RAG layer (`project_memory.py:1-28`), and Horvitz-1999 mixed-initiative confirmation (`understanding.py:4-9`).

### 1. Rule induction — inferring the generating rule (Stage 1)

`orchestrator/rule_induction.py` answers a sharper question than "what was the last value?": *what rule generated the user's values, given their context?* The motivation (`rule_induction.py:1-24`) is generalization — the agent should extrapolate to held-out instances (the other branch of a conditional, another instance on a known level) instead of replaying the last value. Examples are `{"value": <str>, "context": {<key>: <value>}}` dicts, where context is assembled by `executor_agent._example_contexts` (`executor_agent.py:877-901`) from the *sibling* parameter values and level observed in the same recorded example.

Three rule families are induced, in a deliberate priority order:

| Family | Function | Requires | Abstains when |
| --- | --- | --- | --- |
| `conditional` (threshold) | `induce_conditional` (`rule_induction.py:38-70`) | exactly 2 output values, numeric contexts cleanly separated by a cut point | contexts overlap (no clean cut) |
| `conditional` (categorical) | `induce_conditional` (`rule_induction.py:63-69`) | ≥2 categories, ≥2 distinct outputs, **categories that repeat** | any branch is contradictory, or the overfit guard trips |
| `per_context_seq` | `induce_per_context_seq` (`rule_induction.py:73-87`) | ≥2 context groups, ≥1 group with an inducible sequence | an unseen context group at apply time |

**The threshold branch** (`rule_induction.py:50-57`) fires only when there are exactly two output values whose numeric contexts are separable — `max(low) < min(high)` — in which case the cut point is placed midway (`thr = (max(lo) + min(hi)) / 2.0`). This produces a rule like `{"kind":"conditional","mode":"threshold","threshold":1500.0,"below":"Narrow","atleast":"Wide"}`.

**The overfit guard** on the categorical branch is the load-bearing honesty mechanism (`rule_induction.py:59-69`):

```python
if 2 <= len(by_ctx) < len(pairs) and all(len(vs) == 1 for vs in by_ctx.values()):
    mapping = {c: next(iter(vs)) for c, vs in by_ctx.items()}
    if len(set(mapping.values())) >= 2:
        return {"kind": "conditional", "key": key, "mode": "category", "map": mapping}
```

The `len(by_ctx) < len(pairs)` clause requires context categories to **repeat**. Without it, a key whose every value is distinct — a per-instance identifier like `Mark`, or a continuous `Width` — would be "explained" as a condition (e.g. `Frame` "caused" by `Mark` `900→D-100`), which is pure memorization. The inline comment (`rule_induction.py:60-62`) names this exact failure. This guard is not merely a design nicety: commit `6a55a4a` ("fix categorical-conditional overfit found by fake-log end-to-end test") added/hardened it after a live fake-log test tripped the overfit.

**`per_context_seq`** (`rule_induction.py:73-87`) induces an independent arithmetic sequence per context group (Mark per level) by delegating to `executor_agent.induce_sequence_rule`. The ≥2-groups requirement (`rule_induction.py:81-82`) makes the *keying* identifiable — L1-only data cannot distinguish "numbered per level" from "numbered globally," so the inducer refuses to claim per-level scoping from single-group evidence.

**The evidence-bounding discipline.** `induce_rule` (`rule_induction.py:108-122`) tries conditionals first, then context-keyed sequences, and — critically — keeps a conditional only if `_reproduces` confirms it reproduces *every* example with zero error (`rule_induction.py:90-105`). This is the same "validate-and-downgrade" discipline used elsewhere in the pattern agent (`rule_induction.py:16-20`); a rule that mispredicts even one recorded example is discarded and the caller falls back. `per_context_seq` is validated by construction, so `_reproduces` short-circuits to `True` for it (`rule_induction.py:92-93`).

**`apply_rule`** (`rule_induction.py:125-140`) is where honest abstention becomes operational. It returns `None` — never a guess — when the rule cannot determine a value: a threshold/category rule whose key is missing from the live context, a category never seen in training (`rule_induction.py:136`), or a `per_context_seq` group that is brand-new (`rule_induction.py:137-139`). Abstention propagates upward: the resolver then falls back to a flat sequence, and if that too abstains, the agent asks the user. A never-demonstrated branch is *not invented* (`rule_induction.py:8-14`).

### 2. The understanding layer — hypotheses, fingerprints, ledger

`orchestrator/understanding.py` converts the induced latents into plain-language hypotheses the user can accept or correct.

**`describe_understanding(motif, examples)`** (`understanding.py:79-117`) walks the motif's variable parameters, re-induces each rule via `induce_rule` (falling back to `induce_sequence_rule` for a flat sequence), and emits one hypothesis per variable parameter plus the routine's intent (goal/trigger). Each hypothesis is `{key, statement, kind, fingerprint}` with stable keys `rule:<param>`, `intent:goal`, `intent:trigger` (`understanding.py:82`). `_describe_rule` (`understanding.py:32-46`) renders human-readable statements ("You set Frame by width: 'Narrow' when width is below 1500, 'Wide' when it is at least 1500."); note `per_context_seq` statements are *scoped to the groups actually seen* and explicitly promise to ask about new ones (`understanding.py:41-45`) — the rendered claim never over-reaches the evidence.

**Fingerprints** (`_rule_fingerprint` `understanding.py:49-61`, `_seq_fingerprint` `understanding.py:64-67`) are stable signatures of a rule's *structure*, not its wording. Examples: `cond:thr:width:1500:Narrow|Wide`, `pcs:level:L1,L2`, `seq:5:W-|:3`. Their purpose is re-confirmation: memory compares the new fingerprint against the stored one to detect when a re-induced rule *materially changed* (step 1 became step 5, a threshold moved) and therefore a previously-confirmed flag must be invalidated. `_fmt_threshold` (`understanding.py:26-29`) keeps the rendered/fingerprinted cut point consistent with the float `apply_rule` actually classifies on (integer thresholds print plainly, fractional keep one decimal).

**The ledger** — `log_understanding(routine_id, entries, path)` (`understanding.py:120-129`) appends JSONL records to `%LOCALAPPDATA%\RevitPersonalization\logs\understanding_ledger.jsonl` (`understanding.py:22-23`). It is best-effort (`except Exception: pass`) so it never breaks a run. This is a deliberate *thesis artifact*: it makes "understanding" auditable — what was inferred, from how much evidence, and whether the user confirmed or corrected it — rather than merely asserted (`understanding.py:6-9`).

### 3. Active confirmation, auto-demotion, reflection, and the ledger (Stages 3–4)

Confirmation status lives in per-user memory. The governing principle (`project_memory.py:317-321`): *inferred ≠ known*; unconfirmed understanding may suggest but is never silently acted on, preserving the "every write is confirmed" contract.

**`record_understanding`** (`project_memory.py:325-346`) stores/refreshes hypotheses under `routine["understanding"][key]`. New keys start `"proposed"`. For an existing key it refreshes the wording, but if the fingerprint differs (structure changed) and the status was not already `proposed`, it **resets to `proposed` and clears the stale correction** (`project_memory.py:342-345`) — so a `confirmed` flag can never survive onto a structurally different rule.

**`confirm_understanding`** (`project_memory.py:348-365`) applies the verdict and returns the new status:

| User action | Resulting status |
| --- | --- |
| accept (no correction) | `confirmed` |
| reject (no correction) | `rejected` |
| correction text, 1st time | `corrected` (stored, overrides the inferred rule) |
| correction text, ≥ `DEMOTE_AFTER_CORRECTIONS` (=2) | `demoted` |

The **auto-demote** (`project_memory.py:322`, `project_memory.py:360`) is Stage 4's guard against a persistently-wrong rule: once corrected twice, the hypothesis is `demoted` and the agent stops trusting it, falling back to literal/ask.

**`understanding_block`** (`project_memory.py:368-382`) renders *only* `confirmed` and `corrected` entries into the executor prompt — as `CONFIRMED: …` and `The user CORRECTED an earlier guess — follow this instead: …`. `demoted`/`rejected`/`proposed` are intentionally omitted (`project_memory.py:370-372`); the agent must not act on understanding the user disowned or has not confirmed. This block is injected by `to_prompt` (`project_memory.py:511`).

**`reflect(mem)`** (`project_memory.py:410-434`) is the "learning-to-learn" loop: a normalized understanding *signature* (via `_understanding_signature`, `project_memory.py:392-403`) that is `confirmed` in ≥ `MIN_ROUTINES_FOR_GENERALIZATION` (=2) routines is promoted to a cross-routine user-profile prior, so a brand-new routine inherits it instead of re-discovering it. `reflect` **reconciles** — it both adds newly-supported generalizations and *retracts* ones that no longer meet the bar after demotions/rejections — and returns `{'added':[...], 'retracted':[...]}`. Two review-hardened honesty guards (`project_memory.py:417-419`): only `status=='confirmed'` evidence counts (a free-text correction is applied per-routine but never promoted to a confident user-wide prior), and the signature is derived from the agent-generated *statement*, never from the free-text correction string. Generalizations live in a segregated `user['generalizations']` list (`project_memory.py:86`, `project_memory.py:429-433`) so pruning never clobbers user-authored notes; `user_block` renders them as "Cross-routine conventions" (`project_memory.py:455-457`).

**HTTP wiring** (`chatbot/chat_server.py`): `GET /api/understanding` (`chat_server.py:1258-1277`) calls `describe_understanding`, records the hypotheses, and returns each with its stored status/correction — the data behind the mixed-initiative "is this right?" panel. `POST /api/understanding/confirm` (`chat_server.py:1287-1309`) calls `confirm_understanding`, then `reflect`, then writes a ledger record for the verdict plus one per added/retracted generalization (`chat_server.py:1301-1308`). Both endpoints degrade rather than 500 on a corrupt record (`chat_server.py:1276-1277`).

### 4. Project memory — the persistent per-user store

`orchestrator/project_memory.py` is the OpenClaw-style store: file-based, human-readable, loaded into context each session and written back to as the assistant learns (`project_memory.py:1-28`). Identity is resolved best-effort — `REVIT_USER_ID` env override → OS account name → `"default"` (`project_memory.py:46-52`) — and memory is stored per user at `%LOCALAPPDATA%\RevitPersonalization\users\<user_id>\memory.json` with atomic writes (`save`, `project_memory.py:115-124`) and a one-time migration from the legacy global store (`load`, `project_memory.py:99-112`). `_coerce` (`project_memory.py:77-96`) fills defaults and migrates legacy shapes.

Key persisted state per routine (`routine_mem`, `project_memory.py:157-166`):

- **`last_values`** — `{param: value}` last actually set, written by `record_execution` (`project_memory.py:175-185`), which explicitly *rejects* empty/`"none"`/`"null"` values because a stray one would poison the next-in-sequence resolution (`project_memory.py:182-185`).
- **`family_substitutions`** — `{wanted: used}` (`learn_substitution`, `project_memory.py:169-172`), rendered by `to_prompt` so the agent uses the loaded family directly instead of re-thrashing the unmapped one.
- **`compiled_skill`** — the parameterized deterministic replay program distilled from a successful run (`set_compiled_skill`/`get_compiled_skill`, `project_memory.py:194-200`).
- **`corrections`** — cross-run failure lessons mined from the tool trace by `learn_corrections` (`project_memory.py:256-314`) and surfaced high in the prompt by `corrections_block` (`project_memory.py:464-478`).
- **`understanding`** — the Stage-3 hypothesis store described above.

**`to_prompt`** (`project_memory.py:481-515`) assembles the full memory block for the executor: the per-user profile (`user_block`), the "mistakes to avoid" block, the confirmed/corrected understanding block, then per-routine facts (known substitutions, last host wall, **`last_values` with the "user may want the next in sequence" hint**, execution count) and the project's already-loaded families. `user_block` (`project_memory.py:438-461`) renders the "remembers you" layer: name/role hints, preferences, conventions, notes, and cross-routine generalizations.

### 5. How a correction carries forward — the current mechanism

This is the crux, and it was a deliberate fix (commit **`33ac8e5`**, "fix: corrections don't carry forward — anchor Mark scheme to the LIVE model + real tag offset"). The **root cause** was that a correction reached only the LLM prose (and the `understanding_block`), never the deterministic resolver, and was bypassed entirely on the compiled-replay path. `resolve_routine_values` computed the next Mark *only* from the routine's recorded examples (`101,102,103 → 106`); the live model's real marks were read but used *only* for exact-duplicate dedup, so a project scheme like `TU 29 … TU 233` never collided with `106` and the private counter sailed through, ignoring the convention the user kept correcting.

The current mechanism closes the loop through the **live model**, in `executor_agent.resolve_routine_values` (`executor_agent.py:904-1007`). For each variable parameter the resolver tries, in order:

1. A **contextual rule** — `induce_rule` on the example contexts, applied with the live context (`executor_agent.py:944-949`). Abstains (`None`) if under-determined.
2. **Anchor to the live model** (`executor_agent.py:959-985`): if the model already contains values for this parameter (`existing_values`, gathered off-thread via `_existing_param_values`, `chat_server.py:888-896`), those elements **win over the routine's examples even if the examples form a clean rule** (`executor_agent.py:964-974`). It induces a rule from the live values (e.g. constant step) via `induce_sequence_rule`, else — because real schemes have gaps and induction returns `None` — continues from the highest live mark (`TU 233 → TU 234`, `executor_agent.py:975-985`), advancing past any value already in use.
3. Only with **no live values** does it fall back to inducing the scheme from the routine's own examples + `last_values` (`executor_agent.py:986-1006`).

The load-bearing consequence is stated in the code itself (`executor_agent.py:961-963`):

```python
# This also closes the correction loop for free: when the user
# corrects a Mark, that value becomes a real element, so the next resolve reads it and continues.
```

**So the "current mechanism" is: a corrected value becomes a real element in the Revit model, and because the resolver now reads the live model as the authoritative source of the user's scheme, the next resolve *reads that corrected element back and continues from it* (`TU 234 → TU 235`).** The correction does not need a special "apply my correction" pathway in the resolver; it is carried forward mechanically, through the model itself, and — critically — this also works on the compiled-skill replay path that never consults the LLM. Confirmed/corrected understanding is *additionally* surfaced to the LLM via `understanding_block`/`to_prompt`, but the deterministic resolver no longer depends on that prose being read. On a fresh project with no live marks, resolution still falls back to the example rule, so nothing is lost when there is no live evidence to anchor to.

### Relevant file paths

- `C:/Users/DE1E7A/revit-personalization/orchestrator/rule_induction.py` — Stage-1 rule induction (`induce_conditional`, `induce_per_context_seq`, `induce_rule`, `apply_rule`, `_reproduces`, overfit guard at `:59-69`).
- `C:/Users/DE1E7A/revit-personalization/orchestrator/understanding.py` — `describe_understanding`, fingerprints, `log_understanding` ledger.
- `C:/Users/DE1E7A/revit-personalization/orchestrator/project_memory.py` — per-user store, `record_understanding`/`confirm_understanding` auto-demote, `understanding_block`, `reflect`, `last_values`, compiled skills, `family_substitutions`, `to_prompt`/`user_block`.
- `C:/Users/DE1E7A/revit-personalization/orchestrator/executor_agent.py` — `resolve_routine_values` (`:904-1007`), `_example_contexts`, `induce_sequence_rule`, `next_from_rule`, `next_in_sequence` — the runtime resolver that applies learned schemes and closes the correction loop via the live model.
- `C:/Users/DE1E7A/revit-personalization/chatbot/chat_server.py` — HTTP wiring: `/api/understanding` (`:1258-1277`), `/api/understanding/confirm` (`:1287-1309`), and the resolve/write-back call sites (`:884-897`, `:1030-1045`).

Key commits: `3b1f118` (Stage 1 induction), `d96e249` (Stage 3 active confirmation), `083b7c4` (Stage 4 reflection + ledger), `efd31fe` (adversarial hardening), `6a55a4a` (categorical-overfit fix), `33ac8e5` (corrections-carry-forward via live-model anchoring).

## The executor — agentic execution loop

The **executor** (`orchestrator/executor_agent.py`, 1541 lines) is the component that takes a *learned* routine — detected offline by the Pattern/Macro agents, blind to the live model — and actually replays it in the user's running Revit session with live feedback and self-correction. Its design premise is stated in the module docstring:

> The Macro Agent generates a fixed `tool_sequence` OFFLINE, blind to the live model, so it cannot react to "no host wall" or "family not loaded". This executor runs WITH live feedback, so it recovers from exactly those failures.
> — `orchestrator/executor_agent.py:6-8`

It implements the same tool-use loop pattern that Claude Code itself uses: call a tool, feed the *result — including errors —* back to the model, and let it diagnose and retry. Everything (`client`, `dispatch_fn`, `on_event`, `confirm_fn`) is injectable so the loop is unit-testable without the Anthropic API or a live Revit (`executor_agent.py:22-24`; tests in `tests/test_executor_agent.py`).

The remainder of this section documents each mechanism in the order the model encounters it: the tool schemas the model sees, real dispatch, the agentic loop and its self-healing brakes, the cost model, the read-loop guard, value resolution, goal construction, and — the deterministic short-circuit that wraps the whole thing — compiled-skill replay.

### The tool surface the model sees

The tools the executor may use *are* the allowlist — nothing outside `ALLOWED_TOOLS` is dispatchable, and `send_code_to_revit` is never exposed by name (`executor_agent.py:19`, `46-51`, `293`). The surface is assembled in three tiers:

| Tier | Source | Purpose |
|---|---|---|
| `CURATED_SCHEMAS` | hand-written, `executor_agent.py:52-159` | Ergonomic tools for the common routine path: `place_element`, `set_parameter`, `tag_element`, plus model-grounding reads (`get_available_family_types`, `get_active_view`, `inspect_model`, `get_selected_elements`, `pick_point`) |
| `revit_tools.TOOL_SCHEMAS` | `orchestrator/revit_tools.py` | The full plugin surface (create walls/floors/grids/levels/rooms/dimensions, color/override, duplicate, delete, atomic transaction groups, image export, deep queries) **plus grown tools** appended from `grown_tools.json` |
| Gated fallbacks | `executor_agent.py:186-283` | `get_warnings` (fixed read-only snippet), `get_parameters_bulk` (fixed batch-read snippet), and `execute_revit_api` (the raw C# fallback) — appended only if `API_FALLBACK_ENABLED` |

```python
TOOL_SCHEMAS: list[dict] = (
    CURATED_SCHEMAS + revit_tools.TOOL_SCHEMAS
    + ([GET_WARNINGS_TOOL, GET_PARAMS_BULK_TOOL, EXECUTE_API_TOOL] if API_FALLBACK_ENABLED else [])
)
ALLOWED_TOOLS = {t["name"] for t in TOOL_SCHEMAS}
```
— `executor_agent.py:289-293`

**Grown tools.** The full plugin surface is not static. `revit_tools.TOOL_SCHEMAS` is built as `tool_schemas() + grown_schemas()`, where `grown_schemas()` reads `grown_tools.json` (`revit_tools.py:105-121`). This is the sink of the self-extension loop: every *successful* `execute_revit_api` call is a capability gap the agent had to fill with ad-hoc code, so it is appended to `grow_candidates.jsonl` (`executor_agent.py:170-184`, `733`); a distillation step (`tools/grow/promote_fallbacks.py`, referenced at `executor_agent.py:172`) later turns each into a clean parameterized bim-mcp command + schema, which then appears as a first-class grown tool. So the toolset the model sees literally grows over time.

**`get_warnings` — the fixed audited snippet.** The executor was previously *blind* to Revit's own warning list (duplicate Mark, unhosted element, overlap), which a tool can report success on. `get_warnings` fixes this by running a **fixed, audited** read-only C# snippet — the model never authors the code, so the call is confirmation-exempt (`executor_agent.py:215-239`):

```python
GET_WARNINGS_CODE = (
    "var ws = document.GetWarnings();\n"
    "return ws.Select(w => new {\n"
    "    description = w.GetDescriptionText(),\n"
    "    severity = w.GetSeverity().ToString(),\n"
    "    has_resolution = w.HasResolutions(),\n"
    "    failing_ids = w.GetFailingElements().Select(id => id.Value).ToList(),\n"
    "    additional_ids = w.GetAdditionalElements().Select(id => id.Value).ToList()\n"
    "}).ToList();"
)
```

This was added in commit `f3d3c74` ("read Revit warnings/errors/dialogs"). At dispatch time the executor prefers a *dedicated* plugin command and only falls back to this fixed snippet if that command is not deployed (`executor_agent.py:674-697`).

**`get_parameters_bulk` — the audited batch read** (commit `b087761`). `_bulk_params_code` (`executor_agent.py:242-263`) builds another fixed snippet that reads the requested parameters for *every* element of a category in one `FilteredElementCollector` pass. The model only supplies a *sanitized* category and parameter names — the category is stripped to an `OST_*` identifier (`re.sub(r"[^A-Za-z0-9_]", "", …)`) and each name is stripped and length-capped — so it never authors code and the call is a confirmation-exempt read. This is the cure for the "audit 60 doors one-by-one" cost blowup (see the read-loop guard below).

**`execute_revit_api` — the gated last-resort fallback.** When no structured tool fits, the model can write a small C# snippet that is the body of `object Execute(Document document, object[] parameters)` (`executor_agent.py:186-213`). It is gated behind `EXECUTOR_ALLOW_API_FALLBACK` (default on, `executor_agent.py:168`), runs transactionally-and-undoably by default (`transactionMode: "auto"`), and every *write* pauses for user confirmation (see `needs_confirmation` below). The schema description draws the line sharply: "try the structured tools FIRST; read before you write; NEVER delete or modify anything the goal did not ask for."

### Prompt caching on the tool prefix

The tool block (~30 verbose schemas, measured ~19.8K tokens) is identical on every loop iteration and dominates input tokens. A `cache_control` breakpoint on the *last* tool lets the API serve the entire tools+system prefix from cache on iterations 2+ (~0.1× price) (`executor_agent.py:295-319`):

```python
if TOOL_SCHEMAS:
    TOOL_SCHEMAS[-1] = {**TOOL_SCHEMAS[-1], "cache_control": _cache_control()}
```

The TTL is deliberately the **1-hour extended cache** rather than the 5-minute default (`EXECUTOR_CACHE_TTL="1h"`, `executor_agent.py:306-308`), because the loop can stall far longer than five minutes on a `pick_point` — the user has to physically click in Revit — and a 5-minute prefix would expire mid-run and be re-billed at full price. The 1-hour TTL requires the `extended-cache-ttl-2025-04-11` beta header, sent only when actually caching on Claude (`executor_agent.py:1201-1202`). A cache-free copy `TOOL_SCHEMAS_PLAIN` (`executor_agent.py:321-324`) is used for backends that don't support Anthropic caching (Gemini via the LiteLLM proxy), where `cache_control` would be ignored at best and a 400 at worst.

### Real dispatch — each tool → a live plugin call

`real_dispatch(name, args)` (`executor_agent.py:548-744`) executes exactly one tool against the live Revit plugin (`mcp_server.revit_bridge`) and returns a normalized `{success, message, …}` dict. The three write tools carry the executor's hardest-won bug fixes.

**`place_element` — the fix that made placement work at all.** The plugin's `create_point_based_element` resolves what to place from a `FamilyTypeId` or category — its model has *no family-name field* — so sending a family name silently resolved nothing and "succeeded" creating 0 elements (`executor_agent.py:424-427`; the root-cause fix is commit `8abd494`, "place_element created 0 every time"). `place_element` therefore resolves the family *name* to a loaded `FamilyTypeId` itself, via `_resolve_type_id` (`executor_agent.py:436-454`): it queries `get_available_family_types` across `_PLACEABLE_CATEGORIES`, lowercase-matches on `FamilyName`, then prefers an exact `TypeName` match if `type_name` was given.

If the family is genuinely not loaded in *this* model, rather than placing a blind default (which rolls back) or thrashing through retries, it maps to the closest loaded family via `_family_match` (`executor_agent.py:457-474`), scoring by shared lowercased tokens (`len(want & fam) + 0.5 * len(want & typ)`). A positive score auto-substitutes and reports the substitution honestly; a zero score lists the available families and asks (`executor_agent.py:558-574`). This *honest success + family substitution* is commit `5b37656`.

Crucially, after a create that reports success, `place_element` **verifies persistence** — because `create_point_based_element` can report success on a create that *rolled back* (a door/window with no valid host wall), and that false "placed" is what sent the agent thrashing (`executor_agent.py:584-603`):

```python
info = rb._call_plugin("get_element_info", {"elementId": int(eid)})
gone = isinstance(info, dict) and (info.get("Success") is False or "not found" in
       str(info.get("Message") or info.get("message") or info.get("error") or "").lower())
if gone:   # only a DEFINITIVE not-found blocks success; an empty/transient reply is trusted
    return {"success": False, "message":
            "the placement did not persist (the element no longer exists). For a door/window "
            "this usually means there was no host wall — pass host_wall_id from a wall at this "
            "point (get_selected_elements, or pick_point on a wall)."}
```

**`set_parameter`** (`executor_agent.py:608-613`) maps to the plugin's `set_element_parameter` with a single `{name, value}` pair.

**`tag_element` — tag-type resolution + real offset** (commit `7137361` for the resolution, `33ac8e5` for the offset). The plugin's tag auto-find is broken: it compares the *tag family's* category to the *element's* category, which never match (a door tag is `OST_DoorTags`, the door is `OST_Doors`). So the executor resolves the tag type id itself via `_resolve_tag_type_id` (`executor_agent.py:498-516`) — read the element's category, map it through `_TAG_CATEGORY` (`executor_agent.py:480-495`), and pick the first loaded tag family type — and passes it as `tagTypeId`. It also honors an `offset_x`/`offset_y` (mm), defaulting `offsetY=500` so the tag sits clear of the element; the old hardcoded `0,0` silently overrode the user's requested offset, making the preference inert (`executor_agent.py:621-628`).

**Generic dispatch.** Every other exposed plugin command (including grown tools) is dispatched generically — its args *are* the plugin params — via `revit_tools.dispatch` (`executor_agent.py:741-742`). An unknown/disallowed tool returns a failure result rather than raising (`executor_agent.py:744`).

### The agentic loop (`run_executor`)

`run_executor` (`executor_agent.py:1163-1353`) is the heart of the executor. Its signature exposes every injection point and every knob (client, dispatch, event stream, confirmation callback, API-fallback guard, required steps, iteration cap, model, memory block, escalation target, escalation threshold, preflight toggle, and `prior_messages` for persistent sessions).

**Pre-flight grounding.** Before the loop, `_preflight_facts` (`executor_agent.py:1144-1160`) reads the user's current Revit *selection* once and prepends it to the goal, so the model leads with the correct host instead of burning a round-trip discovering it (`executor_agent.py:1204-1209`).

**Persistent session.** If `prior_messages` is supplied (the running tool-use history of earlier tasks in the same chat), the new user turn is appended to it, so the agent *remembers* element ids it created, families it found loaded, and what is already tagged, instead of re-grounding every task (`executor_agent.py:1210-1215`; the persistent-session feature is commit `1f01094`).

**The loop itself** (`executor_agent.py:1225-1353`) is the canonical Anthropic tool-use pattern, bounded by `max_iters` (default `MAX_ITERS = 14`, `executor_agent.py:38`):

```python
for _ in range(max_iters):
    resp = client.messages.create(model=model, max_tokens=1024, system=system_param,
                                  tools=tools_param, messages=messages, ...)
    # accumulate usage (input/output/cache_read/cache_write)
    blocks = _blocks(resp)
    messages.append({"role": "assistant", "content": blocks})   # verbatim, so tool_use ids line up
    tool_uses = [b for b in blocks if b.type == "tool_use"]
    if not tool_uses:
        # completion enforcement (see below), then return
    for tu in tool_uses:
        # allowlist check → guards → dispatch → append tool_result (is_error on failure)
    messages.append({"role": "user", "content": results_content})
    # adaptive escalation check
```

Every `tool_result` sets `is_error` from the tool's `success` flag (`executor_agent.py:1334`) — *this is what lets the model see and diagnose its own failures*. Every step is streamed through `emit(kind, payload)` with kinds `reasoning`/`tool`/`result`/`done`/`error`, so the user watches the self-correction as a Claude-Code-style transcript (and `chat_server.py` captures the full stream to an on-disk reviewable record, `chat_server.py:917-924`).

### Self-healing — the three brakes

The executor is not merely a loop; it embeds three programmatic brakes plus a prompt-level self-correction contract (`EXECUTOR_SYSTEM`, `executor_agent.py:326-419`) that instructs the model to read each tool's `message`, diagnose, and retry, capping at 3 failed attempts on the same step.

**1. The API-fallback nudge** (commit `74b5d22`). The single largest failure mode was the agent knee-jerk dropping to raw `execute_revit_api` whenever a *structured* tool returned an error. The first time it reaches for `execute_revit_api` after working with structured tools, the loop returns `API_NUDGE` (`executor_agent.py:782-789`) *instead of running the code*; the agent must re-affirm (call it again) to proceed (`executor_agent.py:1309-1316`):

```python
elif (guard_api_fallback and name == "execute_revit_api" and not api_reaffirmed
        and not _hit_hosted_placement_gap(tool_calls)):
    api_reaffirmed = True
    result = {"success": False, "message": API_NUDGE}
```

A genuine capability gap still gets through on the second call; a reflex escalation gets redirected. There is one deliberate **exception**: `_hit_hosted_placement_gap` (`executor_agent.py:765-775`) detects that a placement already returned the "created 0 / no element" signature — which means the structured tool genuinely *cannot* host this door/window — so dropping to the API (`NewFamilyInstance` + host) is the legitimate recovery and is *not* nudged. The nudge is re-armed after any non-API tool (`executor_agent.py:1327-1329`), so each fresh escalation is challenged once.

**2. Confirmation gate.** `needs_confirmation` (`executor_agent.py:757-762`) returns True for any `execute_revit_api` call that *writes* (`transactionMode != 'none'`); read-only queries run free. If a `confirm_fn` is wired (`chat_server.py:926-938` pauses the worker thread and surfaces the code to the user for an explicit OK), a declined call returns a "user declined" result telling the model to use a structured tool instead (`executor_agent.py:1317-1320`).

**3. Completion enforcement** (commit `54728d9`). A placement alone is never "done" — a weaker model tends to place the element and declare victory without setting parameters or tagging. When the model stops calling tools, `_incomplete_steps` (`executor_agent.py:1068-1086`) compares the routine's `required` steps against the successful tool calls. Because the agent has several ways to place/set/tag, matching keys off the *sets* `PLACE_TOOLS`, `SETPARAM_TOOLS`, `TAG_TOOLS` (`executor_agent.py:802-805`) — e.g. `place_and_configure` satisfies both a "place" and its parameters. If steps remain:

- Up to `MAX_COMPLETION_NUDGES = 2` times (`executor_agent.py:796`), it re-prompts the model with a concrete `_completion_nudge` naming the missing steps and the placed element id (`executor_agent.py:1256-1263`).
- If the model *still* won't finish, the loop **completes the known steps deterministically itself** on the placed element (`executor_agent.py:1264-1284`) — running `set_parameter`/`tag_element` directly so the routine never ends half-done. (Variable/runtime params with unknown value are left to the user, `executor_agent.py:1269-1270`.)

### The cost model — Haiku-first with escalation

The executor treats model choice as a cost lever. `choose_start_model(motif, routine_entry)` (`executor_agent.py:1126-1141`) returns a `(start_model, escalate_to)` pair:

```python
if not ADAPTIVE_START or llm.is_gemini(ceiling) or llm.resolve(ceiling) == CHEAP_MODEL:
    return ceiling, None                              # free tier, disabled, or already cheapest
warm = bool(r.get("executions", 0) or r.get("last_host_wall_id")
            or r.get("family_substitutions") or r.get("compiled_skill"))
if _is_simple_motif(motif) or warm:
    return CHEAP_MODEL, ceiling
return ceiling, None
```

The policy (commit `3d8aad8`) is: start a routine on the **cheap model** (`CHEAP_MODEL`, resolved from `haiku`, `executor_agent.py:1110`) whenever it is *simple* — `_is_simple_motif` requires every step to be place/set/param/tag/create only (`executor_agent.py:1114-1123`) — **or** memory-*warm* (previously executed, or has a known host / substitution / compiled skill). Only a *cold and complex* routine starts on the ceiling. Adaptive start is skipped entirely on Gemini (free ceiling, where paid Haiku would cost *more*) or when the ceiling already *is* the cheapest model (`executor_agent.py:1131-1132`).

Inside the loop, escalation fires at most once per run: once accumulated failures reach `ESCALATE_AFTER_FAILURES` (default 2, `executor_agent.py:1111`), the model is stepped up to `escalate_to` and the loop continues on the *same* message history — Haiku→Sonnet are both direct-Anthropic, so the existing client serves either (`executor_agent.py:1339-1348`):

```python
if escalate_to and not escalated and llm.resolve(model) != llm.resolve(escalate_to):
    fails = sum(1 for c in tool_calls if not (c.get("result") or {}).get("success"))
    if fails >= escalate_after_failures:
        model = llm.resolve(escalate_to); escalated = True
        emit("reasoning", f"Escalating to {model} after {fails} failed attempt(s)...")
```

Per-run token usage (input / output / cache_read / cache_write) is accumulated (`executor_agent.py:1223`, `1238-1243`) and returned for cost logging (commits `2877f73`, `8058b7b`).

### The READ-loop guard

The most expensive pathology observed was auditing many elements *one at a time* — that pattern hit 605K input tokens (~\$2) for a single request, because each per-element read snowballs the conversation. Two defenses (commit `20bc979`): the system prompt's hard "READ IN BULK, NEVER IN A LOOP" rule pointing at `get_parameters_bulk` (`executor_agent.py:330-335`), and a programmatic cap. `_READ_LOOP_TOOLS` (`get_element_parameters`, `get_element_info`, `get_parameter_definitions`) are counted per run; once the count exceeds `_READ_LOOP_CAP` (default 8, `executor_agent.py:43-44`), the loop short-circuits *that call* with a nudge to use one batched read instead (`executor_agent.py:1299-1308`):

```python
if name in _READ_LOOP_TOOLS and read_loop_count > _READ_LOOP_CAP:
    result = {"success": False, "message":
              f"Stopped: you've read {read_loop_count} elements one-by-one. Do NOT loop "
              "get_element_parameters/get_element_info per element — it is very expensive. "
              "Use ONE execute_revit_api snippet with a FilteredElementCollector ..."}
```

### `resolve_routine_values` — sequence induction + LIVE-model anchoring

Before the loop runs, the concrete value of each parameter the routine sets must be decided. `resolve_routine_values` (`executor_agent.py:904-1007`) does this. A constant recorded value is used as-is; a *variable* one (e.g. `Mark`) becomes the **next value in its observed sequence**. The resolution order, per parameter, is:

1. **Conditional / per-context rule** — `induce_rule`/`apply_rule` (from `orchestrator/rule_induction.py`, commits `ca850b8`, `6c86ab7`, `efd31fe`) can infer a value chosen by a condition on a sibling parameter or level (e.g. Mark numbered *per level*), built from `_example_contexts` (`executor_agent.py:877-901`) and applied against the live `base_ctx`.
2. **LIVE-model anchoring** — this is the key correctness fix (commit `33ac8e5`). The elements *already in the project* are far stronger evidence of the user's real naming convention than the routine's few recorded examples. If the model already uses a scheme (e.g. doors `TU 29`…`TU 233`), the resolver continues **it** (`TU 234`) rather than replaying the routine's private counter (`106`), `executor_agent.py:959-985`:

```python
live = [v for v in (_clean(x) for x in (existing.get(pn) or ())) if v is not None]
if live:
    lr = induce_sequence_rule(live)
    if lr:
        out[pn] = next_from_rule(lr, existing.get(pn)); continue
    nxt = next_in_sequence(_max_in_sequence(live))   # 'TU 233' -> 'TU 234'
    ...
```

This also closes the **correction loop for free**: when the user corrects a Mark in Revit, that value becomes a real element, so the next `resolve_routine_values` reads it back and continues from it — a manual correction is never clobbered by the private counter.

3. **Sequence induction from examples** — `induce_sequence_rule` (`executor_agent.py:844-862`) infers the *generating rule* (shared prefix/suffix, zero-pad width, and a constant numeric **step**) instead of assuming `+1`. It requires ≥3 parsed points and ≥3 distinct numbers with a single constant non-zero diff, so `['W-100','W-105','W-110']` yields step 5 (`W-115`), not `W-111`. `next_from_rule` (`executor_agent.py:865-874`) then emits the next value, *skipping any already in use*.
4. **Fallback** — the simple `next_in_sequence` (`executor_agent.py:808-819`), which increments the last run of digits while preserving prefix/suffix and zero-padding (`D-105`→`D-106`, `W-09`→`W-10`), advancing past any value already in the model.

At every level, uniqueness against `existing_values` is enforced so the executor **never silently assigns a duplicate Mark** (commit `c598ce5`) — Revit permits duplicate marks but flags them as a warning that a BIM reviewer would catch.

### `build_goal` and `required_steps_from_motif`

`build_goal(motif, location, param_values)` (`executor_agent.py:1371-1448`) turns a detected routine into the executor's natural-language goal prompt, reading the Pattern Agent's real step fields (`action_type`, `family_name`, `tag_family_name`, `param_name`/`param_value`) and the richer-workflow extensions (`element_role`/`host_role`, `condition`, `value_expr`, `repeat`, and `motif.elements` for multi-element compounds). Constant sequence values are emitted as "use it as-is", `value_expr` steps as "evaluate against the live model", and missing values as "provided at runtime — ask the user". Loops render through `_render_repeat` ("For EACH …, spaced N mm apart, setting Mark = …"), and conditions as "ONLY IF … —". The prompt closes with the anti-half-done instruction: "Do EVERY step in order … the routine is only done when all steps succeeded."

`required_steps_from_motif(motif, param_values)` (`executor_agent.py:1010-1027`) produces the machine-readable `required` list that completion enforcement checks against — one entry of `{type: place|set_parameter|tag}` per step, with the concrete resolved value filled in for each parameter (commit `252359a` fixed the motif field-name mismatch that had been silently dropping these). This is the bridge that lets the deterministic completion machinery know exactly what "finished" means for *this* routine.

### Compiled-skill deterministic replay (`orchestrator/compiled_skill.py`)

The thesis claim is "pattern → executable automation." Until compiled skills (commit `5a16d21`), replay flattened the motif to English and let the LLM *re-derive* the automation on every run — non-deterministic and re-billed, identical to the free-form copilot path. `compiled_skill.py` (159 lines) closes that gap by **distilling one grounded demonstration into a verified deterministic replay**, without a rewrite. The module docstring is explicit that this is *distillation of one successful trace*, not program synthesis from scratch / a DSL / search — it only ever captures tool calls the agent already made successfully (`compiled_skill.py:16-18`).

The lifecycle, orchestrated in `chat_server.py:899-996`:

1. **First confirmed run:** the agentic executor runs once and succeeds.
2. **Distill:** `synthesize(tool_calls, variable_params)` (`compiled_skill.py:54-89`) walks the *successful* action tool calls (`_ACTION_TOOLS = _PLACE | _SETPARAM | _TAG`, `compiled_skill.py:26-30`) and rewrites each into a parameterized step with named **holes**: `{location}` and `{host_wall}` for the location/host args, `{<VariableParam>}` for a variable parameter's value, and `{eN}` for the id created by the N-th placement (so later set/tag steps reference the right element). Literals (family, type, constant params) pass through unchanged. It returns `None` if there is no placement to anchor the replay (`compiled_skill.py:87-88`). The synthesized skill is persisted via `project_memory.set_compiled_skill` (`chat_server.py:988-994`).
3. **Later runs — deterministic replay:** `chat_server.py:960-976` tries compiled replay *first*. `can_replay(skill, bindings)` (`compiled_skill.py:113-115`) is a precondition check — every *external* hole (excluding `{eN}`, which bind at runtime) must be available in the bindings (params + a known `location` + `host_wall`, assembled at `chat_server.py:904-908`). If so, `run_compiled` (`compiled_skill.py:130-158`) executes the program **via the same `dispatch_fn`, with no LLM**:

```python
for i, step in enumerate(skill.get("steps") or []):
    args = {k: _fill(v, bindings, placed) for k, v in (step.get("args") or {}).items()}
    if any(_holes_in(v) for v in args.values()):     # unresolved hole -> can't replay
        return {"done": False, "compiled": True, "tool_calls": tool_calls, "failed_step": i}
    result = dispatch_fn(tool, args)
    if not (result or {}).get("success"):
        return {"done": False, "compiled": True, "tool_calls": tool_calls, "failed_step": i}
    if tool in _PLACE:
        placed.append(_placed_id(result))            # bind {eN} for later steps
```

`_fill` (`compiled_skill.py:118-127`) resolves `{eN}` from the runtime `placed` list and every other hole from `bindings`, recursing into nested dicts (e.g. `locationPoint`).

4. **Self-healing escalation:** compiled replay is *not* a dead end. The moment any hole can't be bound or any step fails, `run_compiled` stops and returns `done: False` with the `failed_step` index, and `chat_server.py:974-976` **falls back to the full agentic executor** for that run. So the deterministic path handles the happy replay cheaply and non-deterministically-free, while the self-healing agent remains the safety net — the honest novelty the module claims: *motif-guided distillation into verified deterministic replay, with a self-healing agent as escalation, fully local and dependency-free* (`compiled_skill.py:16-18`).

### Outcome verification

Finally — orthogonal to whether the compiled or agentic path ran — `verify_outcome` (`executor_agent.py:1451-1489`, commit `efa78d7`) reads the placed element's parameters *back* from the live model and confirms each intended value actually stuck (a "committed" tool result is not proof the value is right). It also surfaces Revit's *own* warnings naming that element, escalating an Error-severity warning to a hard issue. On a mismatch, `chat_server.py:1006-1016` performs one deterministic repair (re-set the off params) and re-verifies, all reported in the stream.

---

**Key files:**
- `C:/Users/DE1E7A/revit-personalization/orchestrator/executor_agent.py` — the executor (loop, dispatch, cost model, value resolution, goal building)
- `C:/Users/DE1E7A/revit-personalization/orchestrator/compiled_skill.py` — deterministic replay (synthesize / can_replay / run_compiled)
- `C:/Users/DE1E7A/revit-personalization/orchestrator/revit_tools.py` — full plugin surface + grown-tool loading
- `C:/Users/DE1E7A/revit-personalization/chatbot/chat_server.py:890-1016` — the orchestration that sequences compiled replay → agent → verify

## Chatbot, predictor & the live surface

This section documents the *user-facing surface* of the system: the single FastAPI process that renders the assistant, streams every turn, drives execution against the live Revit model, remembers the user across sessions, and proactively predicts the next action. It is the layer where the offline machinery (detector, understanding, project memory, the agentic executor) becomes something a BIM professional actually talks to. All citations are to the code as it stands on `main` (tip `1604b38`).

The surface is deliberately a *single file* — `chatbot/chat_server.py` (2 302 lines) — that is both the HTTP/SSE backend (FastAPI, uvicorn on port 5000) and the browser UI (an inline HTML/JS single-page app served from `/`). It imports the execution brain from `orchestrator/executor_agent.py`, deterministic replay from `orchestrator/compiled_skill.py`, cross-session memory from `orchestrator/project_memory.py`, and the low-level Revit socket from `mcp_server/revit_bridge.py`. The proactive predictor lives in the top-level `predictor.py`.

### 1. Process topology and how a detection reaches the screen

```
Revit 2025/26  ──TCP :8080──▶  revit_bridge.py  ◀──imports──  chat_server.py (FastAPI :5000)
  (C# add-in,     JSON-RPC 2.0        │                          ├─ HTML/JS SPA (served at /)
   observer log)                       │                          ├─ SSE streaming endpoints
                                       │                          └─ imports executor_agent, compiled_skill,
pattern_watcher.py ──POST /api/pattern─┘                                   project_memory, predictor
```

The chatbot never talks to Revit directly for detection; it is *pushed* patterns. `pattern_watcher.py` (auto-spawned as a child process at server boot, `chat_server.py:2276-2298`) periodically re-runs detection over the observer log and calls `chatbot.trigger.notify_pattern(...)` (`chatbot/trigger.py:63`), which `POST`s a JSON payload — `{id, label, count, motif, tool_sequence, examples}` — to `POST /api/pattern` (`trigger.py:86-94`). `notify_pattern` will even cold-start the server (`--no-browser --no-watcher`) if it is not already up (`trigger.py:43-60`); the `--no-watcher` flag is critical — it prevents the spawned server from spawning a *second* watcher and double-billing the detection LLM. The server, in turn, owns the watcher's lifecycle: on boot it force-kills any orphaned `pattern_watcher` (a hard-killed server can't run `atexit`) before launching a fresh one (`chat_server.py:2280-2298`).

The port is configurable but defaults to 5000 (`PORT`, `chat_server.py:56`). Because the server is frequently launched *without* `ANTHROPIC_API_KEY` in its environment (e.g. inherited from Revit, or under `pythonw`), `_load_api_key()` reads the key out of the project `.env` or `%LOCALAPPDATA%\RevitPersonalization\.env` before the Anthropic client is constructed (`chat_server.py:68-98`); otherwise every greeting would fail with "Could not resolve authentication method".

### 2. The pattern store and browsable history persistence

Every detection is a *first-class, persistent record*, not a transient popup. The in-memory store is `_patterns: dict[str, dict]` keyed by a stable id, with `_active_id` naming the record a fresh client loads first, and a per-id `asyncio.Lock` registry `_locks` (`chat_server.py:116-127`). Each record carries its own conversation `history` (raw Anthropic messages), `last_element_id`, `pending_location`, `param_overrides`, `pending_task`, and a `status` ∈ `new | seen | executed | dismissed` (schema documented `chat_server.py:106-118`).

**Stable identity.** `_derive_id()` (`chat_server.py:277-286`) prefers the detector's own routine id; failing that it hashes `label + tool-shape` so the *same* routine re-detected updates its existing entry instead of duplicating it. `POST /api/pattern` (`chat_server.py:619-666`) uses this to either update-in-place (bumping `count`, re-badging `status="new"` when repetitions grow) or insert. Crucially, it does *not* steal focus from a user mid-conversation: it only re-points `_active_id` when nobody is actively engaged with the current record (`chat_server.py:655-663`), which is judged by `_has_user_turns()` — a record counts as "engaged" only once the user has said something beyond the hidden `__INIT__` greeting trigger (`chat_server.py:289-295`).

**Persistence.** The entire store is atomically written (temp-file + `replace`) to `%LOCALAPPDATA%\RevitPersonalization\pattern_history.json` by `_save_history()` (`chat_server.py:313-322`) after every mutation, and restored on boot by `_load_history()`, which back-fills missing fields on older records for forward-compatibility (`chat_server.py:325-343`). This is what lets the left-hand sidebar show a *history* of detections, each re-openable with its full conversation intact — the design goal stated in the module header (`chat_server.py:8-16`).

The history routes:

| Route | Purpose | Line |
|---|---|---|
| `POST /api/pattern` | ingest/update a detection | 619 |
| `GET /api/patterns` | sidebar list, newest first | 675 |
| `POST /api/patterns/{pid}/activate` | switch focus, return `_visible_messages` | 683 |
| `DELETE /api/patterns/{pid}` | drop from history | 705 |

`_visible_messages()` (`chat_server.py:358-371`) is the render filter: it hides the `__INIT__` trigger and strips all control tokens from assistant turns so the user never sees the machinery.

### 3. The conversational turn: SSE streaming and the control-token protocol

The heart of the chat is `_stream()` (`chat_server.py:509-583`), an async generator that (a) appends the user turn to `rec["history"]`, (b) streams Claude's reply token-by-token as Server-Sent Events (`data: {"t": chunk}`), and (c) persists the assistant turn. Everything runs **under the record's lock** (`chat_server.py:513`) so two concurrent requests to the same pattern cannot interleave and produce non-alternating roles (which Anthropic 400s on). On any error it rolls back the dangling user turn to keep history alternating, then surfaces the error in-band so the UI unlocks rather than hanging (`chat_server.py:529-538`). The conversational model is Sonnet by default (`MODEL = llm.pick("CHATBOT_MODEL", "claude-sonnet-4-6")`, `chat_server.py:61`), chosen because confirm/execute turns are short and cheap.

The assistant does not call tools directly. Instead it emits **inline control tokens** on their own lines, which `_stream()` parses out of the completed reply and the browser translates into actions. The full token grammar lives in `_TOKEN_RE` (`chat_server.py:129-132`) and is taught to the model in the system prompt (`chat_server.py:378-461`):

| Token | Meaning | Server handling |
|---|---|---|
| `##EXECUTE##` | user confirmed + location known | `action="execute"` → client calls `/api/execute-smart` |
| `##DISMISS##` | user declined | sets `status="dismissed"` |
| `##PICK##` | user wants to click the spot in Revit | `action="pick"` → `/api/pick` blocks on the human click |
| `##LOCATION:x,y,z##` | typed coordinates (metres) | parsed into `rec["pending_location"]` (`chat_server.py:558-564`) |
| `##PARAM:Name=Value##` | user typed a parameter value | stored in `rec["param_overrides"]` (`chat_server.py:554-555`) |
| `##REMEMBER:fact##` | a durable user preference/convention | persisted to project memory (`chat_server.py:544-550`) |
| `##TASK:<nl>##` | a free-form request beyond the routine | stored in `rec["pending_task"]` → `/api/execute-task` (`chat_server.py:568-570`) |
| `##ISOLATE## / ##ZOOM## / ##SELECT##` | post-execution view ops on last element | routed to `operate_element` |

The completion frame `data: {"done": true, "action": ...}` (`chat_server.py:573-583`) tells the browser which follow-up to fire. On the client, `consumeStream()` accumulates the `t` deltas, live-renders them with tokens stripped (`stripTokens`, `chat_server.py:1570`), and `handleAction()` dispatches on the action (`chat_server.py:1864-1872`). This token protocol is the key design decision of the surface: it keeps the *conversational* model (cheap Sonnet) decoupled from the *execution* model (the agentic executor), so the chat turn is pure natural language plus a machine-readable intent marker.

The `##PICK##` path deserves emphasis because door/window placement requires a host wall. `##PICK##` → `runPick()` (`chat_server.py:1874-1898`) → `POST /api/pick` → `revit_bridge.pick_point()`, which blocks on the human click with a long 190 s socket timeout (`revit_bridge.py:136-148`) and returns millimetre coordinates; the server stores them in `pending_location` and the client immediately chains into `runExec()` (`chat_server.py:1887`).

### 4. `/api/execute-smart`: the value-resolution → goal → agentic-execution pipeline

This is the load-bearing endpoint (`chat_server.py:859-1052`; introduced in `b9e6103`, "self-healing agentic executor"). It is an SSE stream that reproduces the routine in the live model, **self-heals** on failure (no host wall → ask the user to pick; family not loaded → substitute the closest), and streams its reasoning, tool calls, and results so the chat shows a Claude-Code-style self-correction transcript. Its `gen()` generator proceeds in stages.

**Stage A — resolve the value for every parameter.** Before any LLM runs, the server decides the concrete value each parameter should take. Constants stay as recorded; *variable* parameters (typically `Mark`) are advanced to the *next* value in their observed sequence. Three inputs feed this:

- `_last_vals` — the value the routine last set, read from project memory (`chat_server.py:887`).
- `_existing` — **all** values already present in the live model for the routine's variable params, via `_existing_param_values()` (`chat_server.py:191-217`), so a computed `Mark` never collides with one already in the model.
- `_ctx` — best-effort live context (the active level) from `_live_context()` (`chat_server.py:220-232`), enabling per-level / conditional rules at runtime.

`_existing_param_values` is now a **single bulk read** (commit `55bc2f4`): it builds the fixed audited snippet behind `get_parameters_bulk` via `executor_agent._bulk_params_code(cat, names, False)` and issues *one* `send_code_to_revit` call reading every element of the category (`chat_server.py:203-205`). This replaced an earlier capped per-element loop that sampled only the first ~50 elements and consequently handed the resolver a stale maximum — producing duplicate Marks. The function is synchronous and dispatched off the event loop via `asyncio.to_thread` (`chat_server.py:893-894`), degrading to `{}` on any failure so a dead Revit never blocks a run.

These three feed `resolve_routine_values(motif, examples, last_values, existing_values, context)` (`executor_agent.py:904-914`), which returns `{param_name: value}`. The resolver applies induced conditional / per-context rules when found, otherwise falls back to next-in-sequence from the max of `{last_values, examples, existing}`. Finally, `param_values.update(rec.get("param_overrides") or {})` (`chat_server.py:897`) lets a value the user typed in chat *win* over the auto-sequence.

**Stage B — compiled-skill deterministic replay (no LLM).** If this routine was previously distilled into a program *and* its holes can be bound (`param_values`, a known `location`, and the remembered `last_host_wall_id`), the server replays it **without any LLM**. `_skill = pm.get_compiled_skill(...)` and `_bindings` are assembled (`chat_server.py:902-908`); if `compiled_skill.can_replay(_skill, _bindings)` (`chat_server.py:961`) the server runs `compiled_skill.run_compiled(...)` deterministically via `real_dispatch` (`chat_server.py:964-965`), tagging the result `model="compiled"`. A compiled skill is a small parameterised JSON program of real `dispatch` calls with named holes (`{location}`, `{host_wall}`, a per-step resolved `{Mark}`, and `{e0}/{e1}` element ids created earlier in the same program) — see the design note in `compiled_skill.py:1-19`. If replay can't finish, execution falls through to the agent (`chat_server.py:974-976`).

**Stage C — the agentic executor (and skill distillation).** When there is no skill, holes are unbindable, or replay failed, the server builds a natural-language goal and runs the self-healing loop:

```python
run_executor(build_goal(motif, location, param_values),
             on_event=on_event, confirm_fn=confirm_fn, memory_block=memory_block,
             required=required_steps_from_motif(motif, param_values),
             model=start_model, escalate_to=escalate_to)          # chat_server.py:980-984
```

`build_goal()` (`executor_agent.py:1371`) renders the motif's ordered steps (place / set-param / tag, plus compound multi-element workflows) into an English goal, substituting the resolved `param_values`. `run_executor()` (`executor_agent.py:1163-1189`) is the tool-use loop: it grounds with a read-only pre-flight (the user's current Revit selection, `_preflight_facts`), then iterates, streaming `on_event(kind, payload)` with `kind ∈ {reasoning, tool, result, done, error}`.

**Cost-adaptive model selection.** `choose_start_model(motif, routine_entry)` (`executor_agent.py:1126-1141`; commit `9e4f56f`, later `3d8aad8`) starts a *simple or memory-warm* routine on cheap Haiku and only escalates to the Sonnet/Opus ceiling on real difficulty (after N tool failures); a cold + complex routine starts on the ceiling. This is a no-op on Gemini's free tier. This is what keeps a routine execution from costing what a naive "always-Sonnet, audit 60 doors" run would.

On success the agent's trace is distilled into a compiled skill for next time: `compiled_skill.synthesize(result["tool_calls"], set(_var_params))` → `pm.set_compiled_skill(...)` (`chat_server.py:988-996`). This is programming-by-demonstration: the first confirmed run teaches the deterministic replay used by every subsequent run.

**Stage D — verify, learn, and clear one-shot overrides.** After execution the placed element id is captured (`placed_element_id`, `chat_server.py:997`) and status set to `executed`. A **deterministic outcome verifier** (`verify_outcome`, `executor_agent.py:1451-1489`; commit `efa78d7`) reads the parameters *back* out of the model and confirms they stuck; on a mismatch it performs one deterministic repair (re-`set_parameter` the off params) and re-verifies, all reported in the stream (`chat_server.py:1006-1018`). A committed tool result is not treated as proof the value is right. The run is logged twice: a compact scannable record (`_log_executor_run`, cost-attributed at the *actual* model's rates via `shared/llm`) and a full reviewable transcript with every reasoning line, tool args, and full result (`_log_executor_transcript`, `chat_server.py:1020-1024`). Then `pm.learn_from_run` and `pm.learn_corrections` write back what the executor learned — family substitutions, the host wall, values, and the *mistakes* to avoid next run (`chat_server.py:1028-1037`).

Finally the **one-shot param override** is cleared (`chat_server.py:1039-1045`; commit `d6d137b`). This fixes a real bug: a chat-typed `##PARAM:Mark=101##` must mean "*this* placement", not a permanent pin. Because `learn_from_run` has already recorded 101 into `last_values`, the sequence naturally continues (101 → 102 → 103); leaving the override in place would freeze every future placement at 101 ("you made the mark 101, it should be 102"). So on a successful run the override dict is emptied.

**The confirmation gate.** Any model-mutating raw Revit-API snippet must be OK'd by the human. `confirm_fn(name, args)` (`chat_server.py:926-938`) registers a pending confirmation, emits a `confirm` SSE event carrying the code, and *blocks the executor worker thread* on a `threading.Event` (300 s timeout → treated as decline). The browser renders an Approve/Reject card; `POST /api/execute-confirm` (`chat_server.py:1060-1068`) resolves the event. The thread→async bridge throughout uses `loop.call_soon_threadsafe(queue.put_nowait, ...)` because `run_executor` runs synchronously in a worker thread (`chat_server.py:919-924`), and `_stream_task()` drains that queue to SSE with a ~5 s heartbeat to keep the stream warm while blocked on a confirmation (`chat_server.py:940-952`).

### 5. `/api/execute-task`: the free-form conversational agent (Pillar C)

`##TASK:<nl>##` routes an arbitrary natural-language request — "how many fire doors on level 2?", "renumber the doors on L2 by room" — to `POST /api/execute-task` (`chat_server.py:1090-1192`), which runs the *same* agentic executor but on `build_freeform_goal(task, context)` rather than a learned routine. Read-only questions are answered via query tools without mutating the model; writes still pass the confirmation gate. Two things make it conversational rather than one-shot: (a) the last six chat turns are fed in as `context` so the agent doesn't redo completed work (`chat_server.py:1110-1116`), and (b) a per-conversation **executor session** (`_exec_sessions`, bounded to 16 turns by `_trim_session`, `chat_server.py:1074-1088`) preserves the running tool-use history so consecutive tasks continue the same agent — reused only if the prior session ended cleanly on an assistant turn (else a new user turn would 400). The outcome is merged back into the trailing assistant turn of the chat history so the next turn knows what was done (`chat_server.py:1173-1186`).

### 6. `predictor.py`: proactive next-action prediction (Pillar B)

Where `/api/execute-smart` replays a *confirmed* routine, the predictor anticipates the *next step while the user is mid-routine*. `predictor.py` (126 lines; commit `bcb3d44`) is deliberately **deterministic and free** — no LLM in the hot path, because prediction is about routines the user *already* repeats.

`current_prefix(records)` (`predictor.py:49-66`) extracts the in-progress episode from the live event log: the actions on the element the user is *currently* working on — from the most-recent `Place` up to (not including) the next `Place`, collecting the matching `SetParam`/`Tag` actions on that element id. `NextActionPredictor.predict(prefix, intents)` (`predictor.py:80-110`) tokenises the prefix with the *same* `detector._common.token` used by detection and execution (so "the same step" is consistent everywhere, `predictor.py:11-12`) and prefix-matches it against the user's learned `CandidateRoutine`s:

- an **exact** typed-token prefix match (`ctok[:n] == ptok`) wins, highest support first, at full confidence;
- otherwise an **action-type** prefix match (placed a door → usually set a param + tag) at half confidence (`predictor.py:104-109`).

The result is a `Prediction` (`predictor.py:22-46`) whose `next_actions` are the remaining steps and whose `headline` is the inline chip text. When the routine's *intent* (the WHY/WHEN, from the understanding pipeline) is known, the headline hedges it as an unconfirmed hypothesis — "…(looks like: to keep the door schedule complete?)" (`predictor.py:44-46`) — never asserting it.

`GET /api/predict` (`chat_server.py:1195-1215`) wires this to the live log: it loads real action records and candidate routines off-thread (`load_real_action_records`, `list_candidate_routines`), harvests any already-extracted `motif.intent` per pattern to explain the chip, and returns the prediction JSON. The browser polls it every 6 s (`setInterval(pollPredict, 6000)`, `chat_server.py:2104`); `pollPredict()` (`chat_server.py:2061-2073`) renders the `💡 headline` chip with "Open routine" (which `switchTo`s the predicted routine so the user can Execute it) and a dismiss button — and it *never* interrupts a live turn (`chat_server.py:2062`).

### 7. Persistent per-user memory (OpenClaw-style) and the memory panel

The assistant remembers who it is working with across sessions via `orchestrator.project_memory` (aliased `pm`). Two capture channels feed it: (a) `##REMEMBER:fact##` from any chat turn is persisted as a preference (`pm.add_preference`, `chat_server.py:544-550`; commit `65e7ce3`), and (b) `pm.learn_from_run` / `pm.learn_corrections` after every execution write back families, values, host walls, and corrections. `_user_memory_prefix()` (`chat_server.py:441-448`) prepends the user's profile block to *every* system prompt (`_build_system`, `chat_server.py:464-502`), and `pm.to_prompt(mem, routine_id)` steers each executor run with the routine-scoped memory block.

The memory panel is exposed by `GET /api/memory` (`chat_server.py:1219-1236`), returning the user's name/role hints, preferences, conventions, notes, learned routines (with execution counts) and loaded families. The browser renders it in a side panel via `loadMemory()` (`chat_server.py:1611-1636`), refreshed after every chat turn, execute, or task (so a run that taught a new family shows immediately). Data control is first-class (GDPR-style): each preference/note has a ✕ that calls `POST /api/memory/forget` to delete it (`chat_server.py:1243-1254`, `forgetMem` `chat_server.py:1637-1641`).

A related mixed-initiative surface is the **understanding panel**: `GET /api/understanding` (`chat_server.py:1258-1277`) returns the induced rules + intent the agent *thinks* it understands about a routine with their confirmation status, and `POST /api/understanding/confirm` (`chat_server.py:1287-1309`) records the user's verdict (confirm / correct), triggering a cross-routine `pm.reflect()` generalisation pass and appending to the understanding ledger for the thesis evaluation.

### 8. The Revit bridge (`revit_bridge.py`, TCP :8080)

Every model interaction ultimately funnels through `mcp_server/revit_bridge.py`, which speaks JSON-RPC 2.0 over a raw TCP socket to the C# Revit add-in on `localhost:8080` (`REVIT_PLUGIN_PORT`, `revit_bridge.py:60-62`). `_call_plugin(command, params, timeout)` (`revit_bridge.py:69-133`) opens a fresh connection per call, reads until it has a complete JSON response, and — importantly for reliability — retries the *connection* a couple of times with backoff, because Revit's `IExternalEvent` loop transiently refuses connections mid-cycle even while open (this previously surfaced as `place_element` randomly failing "not reachable", `revit_bridge.py:88-98`). The chat server imports exactly four bridge entry points (`chat_server.py:48`): `execute_shortcut` (blind sequence replay, used by the legacy `/api/execute`), `_extract_element_id` (parses the placed id out of the `{Success, Response:[123]}` envelope, `revit_bridge.py:464`), `_call_plugin` (used for `operate_element` view ops and the bulk read), and `pick_point` (the blocking human-click). The agentic executor's own `real_dispatch` (`executor_agent.py:548`) is the richer tool router used during self-healing runs. Because every bridge call is a blocking TCP round-trip, the async endpoints wrap them in `asyncio.to_thread` (e.g. `chat_server.py:774`, `843`, `893`) so SSE streams and the sidebar/predict polls never freeze.

### 9. Design synthesis

The user-facing surface implements three complementary "pillars" over one shared execution brain and one shared tokeniser:

- **Pillar A (reactive):** a detected routine → conversation → `##EXECUTE##` → `/api/execute-smart`, which prefers *free deterministic* replay of a compiled skill and only falls back to the *cost-adaptive* self-healing agent, verifying outcomes and learning from every run.
- **Pillar B (proactive):** `predictor.py` + `/api/predict`, a zero-cost prefix-match that offers the next step mid-routine, explained by the induced intent.
- **Pillar C (open-ended):** `##TASK##` → `/api/execute-task`, the full Revit tool surface over arbitrary natural language, with cross-task session memory.

Binding them is (1) a control-token protocol that keeps the cheap conversational model decoupled from the expensive execution model, (2) per-record locking + atomic history persistence that makes every detection a durable, re-openable conversation, and (3) a persistent per-user memory that closes the loop — what the assistant learns from one run (values, families, host walls, corrections, distilled skills, confirmed understanding) steers every subsequent turn, so the personalization genuinely accrues across sessions rather than resetting each time.

## bim-mcp — the owned Revit MCP server

`bim-mcp` (`C:/Users/DE1E7A/bim-mcp`) is the thesis's owned execution backend: a Revit add-in that exposes the Revit API to the Python orchestrator (`revit-personalization/mcp_server/revit_bridge.py`) over a JSON-RPC 2.0 socket on `localhost:8080`. It was built to *replace* the upstream `mcp-servers-for-revit` fork, whose only fault was that it could not be built on this machine: it depended on the NuGet packages `RevitMCPSDK` and `Nice3point.Revit.*`, which the corporate SSL proxy blocks. `bim-mcp` removes that dependency by **vendoring** the SDK surface as plain source and referencing the Revit API through local-DLL `HintPath`s, so `dotnet build` succeeds offline with no CI round-trip (`README.md:7-19`). That offline, seconds-long build is the enabling precondition for the self-extension ("grow") loop, in which an agent codegens a new command, compiles it locally, and hot-loads it into a live Revit.

The solution is three assemblies plus a tooling folder, developed across 16 commits from `81445fa` (scaffold, 2026-06-28) to `b219176` (grow loop closed end-to-end, 2026-06-30). It is licensed MIT as a derivative of the fork (`NOTICE:1-13`).

| Project | Assembly | Role |
| --- | --- | --- |
| `src/Sdk` | `BimMcp.Sdk.dll` | Vendored interfaces, `ExternalEventCommandBase`, JSON-RPC POCOs. Its own assembly for reflection type-identity. |
| `src/Plugin` | `BimMcp.Plugin.dll` | The `IExternalApplication` host: socket server, command loading, config, ribbon, self-extension. |
| `src/CommandSet` | `BimMcp.CommandSet.dll` | The 34 concrete commands (Command + EventHandler + Model triads). |
| `tools/grow` | (Python/JS) | The Tool Engineer grow/repair pipeline (`grow_command.workflow.js`, `build_command.py`, `call.py`). |

### The wire contract (JSON-RPC 2.0)

The contract is defined once in `src/Sdk/JsonRpc/JsonRpcModels.cs` and must match the Python client byte-for-byte (`JsonRpcModels.cs:6-11`). All models use `[JsonProperty]` lowercase names:

- **Request** — `{"jsonrpc":"2.0","method":<m>,"params":<obj>,"id":<str>}` (`JsonRPCRequest`, `JsonRpcModels.cs:13-22`). `IsValid()` requires `jsonrpc == "2.0"` and a non-empty method.
- **Success** — `{"jsonrpc":"2.0","id":<str>,"result":<token>}` (`JsonRPCSuccessResponse:24-31`).
- **Error** — `{"jsonrpc":"2.0","id":<str>,"error":{"code":<int>,"message":<str>,"data":<token?>}}` (`JsonRPCError:33-38`, `JsonRPCErrorResponse:40-47`). The client reads `result` on success and `error.message` on failure.

Error codes follow the JSON-RPC spec (`JsonRPCErrorCodes:49-55`): `-32700` parse, `-32600` invalid request, `-32601` method-not-found, `-32603` internal. `SocketService.ProcessJsonRPCRequest` maps each failure class onto these codes (`SocketService.cs:139-170`).

A second, nested contract lives at the *result* layer. Most commands return an anonymous `{ success, ... }` object, but the ported command surface uses the canonical **PascalCase** `AIResult<T>` shape — `{ Success, Message, Response }` (`src/CommandSet/Models/Common/AIResult.cs:7-12`) — whose casing "must not drift" from the upstream commandset the Python client already parses. Because `SocketService` serializes results verbatim via `JToken.FromObject` (`SocketService.cs:172-180`), the PascalCase keys reach the client exactly as declared.

**The ExternalEvent Raise→WaitForCompletion handshake.** The Revit API is single-threaded: it may only be touched on Revit's UI thread. But the socket server accepts each client on its own worker thread (`SocketService.cs:100-137`). The bridge between the two is `ExternalEventCommandBase` (`src/Sdk/Base/ExternalEventCommandBase.cs`), the "load-bearing correctness path — get it right or every command times out" (`ExternalEventCommandBase.cs:6-13`):

1. The `ExternalEvent` is created **once**, in the command's constructor (`ExternalEvent.Create(handler)`, line 24), because commands are instantiated at startup on the UI thread — a valid context for `ExternalEvent.Create`.
2. On a request, `Execute` runs on the socket worker thread and calls `RaiseAndWaitForCompletion(timeout)` (lines 32-36): `_externalEvent.Raise()` (thread-safe) schedules the handler onto the UI thread, then `Handler.WaitForCompletion(timeout)` **blocks** the worker.
3. The handler's `Execute(UIApplication)` runs on the UI thread, does all Revit work, and signals a `ManualResetEvent` in a `finally`; `WaitForCompletion` returns `true`, and the worker serializes the result.

Every concrete command follows this: e.g. `SayHelloCommand.Execute` locks, sets inputs, and returns only if `RaiseAndWaitForCompletion(15000)` succeeds, else throws `TimeoutException` (`SayHelloEventHandler.cs:13-24`, `SayHelloCommand.cs:19-30`). Timeouts are per-command (15 s for `say_hello`, 30 s for `reload_commands`, 60 s for `send_code_to_revit`).

### src/Sdk — the vendored SDK, and why it must be its own assembly

`BimMcp.Sdk` is a small set of plain interfaces and POCOs that replace the blocked `RevitMCPSDK` NuGet (introduced in `e97b9e7`). It contains:

- **Interfaces** — `IRevitCommand` (the dispatch contract: `CommandName` + `Execute(JObject, string)`), `IWaitableExternalEventHandler` (`IExternalEventHandler` + blockable `WaitForCompletion`), `ICommandRegistry`, `IRevitCommandInitializable` (optional `Initialize(UIApplication)` injection), `ILogger`.
- **Base** — `ExternalEventCommandBase` (above) and `RevitVersionAdapter`, which reads `Application.VersionNumber` and checks per-command version support (`RevitVersionAdapter.cs:12-21`).
- **JSON-RPC POCOs** — `JsonRpcModels.cs` (above).
- **Exceptions** — `CommandExecutionException` carrying a JSON-RPC code + data.

**Why its own assembly (reflection type-identity).** Both the Plugin's `CommandManager` and the runtime `ExtensionLoader` discover commands by reflection: `typeof(IRevitCommand).IsAssignableFrom(type)` (`CommandManager.cs:90`, `ExtensionLoader.cs:36`). In .NET, a type's identity is `(assembly identity, namespace-qualified name)` — the *same* `IRevitCommand` source compiled into two different DLLs produces two **incompatible** types, and the `IsAssignableFrom` check silently fails. By factoring the SDK into a single `BimMcp.Sdk.dll` that ships once and is referenced with `Private=False` everywhere, the Plugin, the CommandSet, and every hot-loaded "grown" DLL all bind to the *same* loaded `BimMcp.Sdk` instance, so their `IRevitCommand`/`ExternalEventCommandBase` are the same runtime type and reflection discovery holds. This is stated explicitly in the grown-command template: references are `Private=false` "so the grown assembly binds to the running instances → type identity holds for reflection discovery" (`tools/grow/template/GrownCommand.csproj:2-5`), and `build_command.py` resolves the *deployed* `BimMcp.Sdk.dll` precisely so a grown command "references the SAME SDK instance that's loaded in Revit" (`build_command.py:6-10, 32`).

The SDK targets `net8.0-windows`, `LangVersion 12`, `x64`, and references the Revit API locally (`BimMcp.Sdk.csproj:5-27`, see build recipe below).

### src/Plugin — the IExternalApplication host

**Entry point.** `Application : IExternalApplication` (`src/Plugin/Core/Application.cs`) is the add-in the `.addin` manifest points to (`BimMcp.addin:3-10`, `FullClassName = BimMcp.Plugin.Core.Application`). On `OnStartup` it best-effort builds a ribbon panel on the shared "BIM Personalization" tab (a ribbon failure must never disable the add-in, so it is wrapped in try/catch, lines 14-38), then subscribes to `ApplicationInitialized`. On that event it constructs a `UIApplication`, `Initialize`s and `Start`s the socket server (`Application.cs:44-61`) — so the server **auto-starts** on launch. `OnShutdown` stops it (lines 63-67).

**SocketService (:8080).** `src/Plugin/Core/SocketService.cs` is a singleton JSON-RPC 2.0 TCP server derived from the fork but trimmed (no separate `CommandExecutor`; commands own their `ExternalEvent`). `Initialize` (lines 42-66) reads the Revit version, loads config, resolves the port (config or 8080), runs `CommandManager.LoadCommands()`, installs `RevitDialogSuppressor`, loads grown extensions, and registers `reload_commands`. `Start` opens a `TcpListener` on `IPAddress.Any:port` with a background listener thread (lines 68-84); each accepted client is handled on its **own** background thread (`HandleClientCommunication`, lines 115-137), reading UTF-8 into an 8 KB buffer and writing the serialized response back. `ProcessJsonRPCRequest` (lines 139-170) deserializes, validates, looks up the command in the registry, invokes it, and wraps the result — catching `JsonException` → parse error and any other exception → internal error.

**CommandManager.** `src/Plugin/Core/CommandManager.cs` reads `commandRegistry.json` and, for each **enabled** and **version-compatible** entry, `Assembly.LoadFrom`s the DLL, finds the `IRevitCommand` type, instantiates it, and registers it (lines 34-127). Instantiation tries three shapes in order: `IRevitCommandInitializable` (parameterless ctor + `Initialize(uiApp)`), a `ctor(UIApplication)`, or a parameterless ctor (lines 94-106). It matches the discovered `CommandName` against the config entry before registering (line 108). An `{VERSION}` token in an assembly path is substituted with the running version (lines 58-59).

**RevitCommandRegistry.** `src/Plugin/Core/RevitCommandRegistry.cs` is a trivial `Dictionary<string, IRevitCommand>` implementing `ICommandRegistry` — populated at startup, read per request via `TryGetCommand` (lines 9-23).

**ConfigurationManager / PathManager.** `ConfigurationManager` deserializes `commandRegistry.json` into a `FrameworkConfig` (`commands[]` + `settings.port`) (`ConfigurationManager.cs:23-44`, `FrameworkConfig.cs`, `CommandConfig.cs`, `ServiceSettings.cs`). `PathManager` (`src/Plugin/Utils/PathManager.cs`) resolves all deployed folders relative to the plugin DLL: `Commands\` (command DLLs + registry), `Commands\Extensions\` (grown DLLs), `Logs\`, and auto-creates a default `commandRegistry.json` if missing (lines 19-54).

**Ribbon + Command Settings.** Two push buttons are added (`Application.cs:20-33`): "Revit MCP Switch" → `MCPServiceConnection` (an `IExternalCommand` that toggles the server on/off, `MCPServiceConnection.cs:11-34`), and "Settings" → `SettingsCommand` → `CommandSettingsWindow` (`SettingsCommand.cs:14-28`). `CommandSettingsWindow` (`src/Plugin/UI/CommandSettingsWindow.cs`) is a code-built (no-XAML) WPF window listing every command with an Enable checkbox and description; Save writes the choices back to `commandRegistry.json` (lines 77-91). Only enabled commands are loaded and answered on `:8080`.

### src/CommandSet — the 34 commands

`BimMcp.CommandSet.dll` holds the concrete command surface, ported from the fork in `369fcfd` ("the FULL command surface... 33 commands") and grown since. The deployed `commandRegistry.json` registers **34** methods; `reload_commands` is registered separately at runtime by the Plugin (`SocketService.cs:62`), for **35** total wire methods.

Each command is a triad: a **Command** (`: ExternalEventCommandBase`, in `Commands/`), an **EventHandler** (`: IWaitableExternalEventHandler`, in `Services/`) that does the UI-thread work, and typed **Models** (in `Models/`). The 34 registered commands:

| Category | Commands |
| --- | --- |
| Create | `create_grid`, `create_level`, `create_room`, `create_line_based_element`, `create_point_based_element`, `create_surface_based_element`, `create_structural_framing_system`, `create_dimensions` |
| Query / read | `get_current_view_info`, `get_current_view_elements`, `get_selected_elements`, `get_available_family_types`, `get_element_info`, `get_element_parameters`, `get_parameter_definitions`, `ai_element_filter`, `analyze_model_statistics`, `get_material_quantities`, `get_warnings` |
| Modify | `set_element_parameter`, `operate_element`, `duplicate_element`, `delete_element`, `color_splash`, `place_and_configure` |
| Data extraction | `export_room_data`, `export_view_image` |
| Tag / annotate | `tag_element`, `tag_rooms`, `tag_walls` |
| Atomic / composite | `execute_transaction_group`, `place_and_configure` |
| Dynamic code | `send_code_to_revit` |
| Misc / test | `say_hello`, `pick_point` |

Interactivity is deliberately curtailed for headless operation: all Chinese text and `TaskDialog.Show` pop-ups were stripped from the ported set in `b6d5b65`. A read-only `DialogWatcher` (`src/CommandSet/Services/DialogWatcher.cs`) still *observes* dialogs into a bounded ring buffer so `get_warnings` can surface them (lines 10-51). The `da32355` commit fixed `tag_element`'s auto-family selection: it had matched a tag family by `FamilyCategoryId == element category`, but a tag family's category is e.g. `OST_WallTags`, never `OST_Walls`, so it never matched; the fix maps element category → its `*Tags` category with a Multi-Category-tag fallback (`Services/AnnotationComponents/TagElementEventHandler.cs:67-86`).

### send_code_to_revit — the dynamic-code escape hatch

`send_code_to_revit` (`Commands/ExecuteDynamicCode/ExecuteCodeCommand.cs`, handler `ExecuteCodeEventHandler.cs`) compiles and runs arbitrary C# against the live model — the mechanism a capability gap is first prototyped through before being crystallized into a compiled command. The command requires a `code` param, accepts optional `parameters` and `transactionMode` (`auto`|`none`), and uses a 60 s timeout (`ExecuteCodeCommand.cs:26-48`).

**The `document` variable + must-return contract.** The handler wraps the user code into a fixed entry point (`ExecuteCodeEventHandler.cs:96-116`):

```csharp
public static object Execute(Document document, object[] parameters)
{
    // User code entry point
    {code}          // ← user code is inlined here
}
```

So the injected code has an in-scope `Document document` (and `object[] parameters`), and — because it is the body of a non-void method — **must contain a `return`** yielding a JSON-serializable value. In `auto` mode the call is wrapped in a `Transaction`; in `none` mode it runs read-only (lines 57-79). The result is `JsonConvert.SerializeObject`'d into `ExecutionResultInfo { success, result, errorMessage }` (lines 81-88, 181-191).

**Curated-references performance fix (`da32355`).** The original implementation compiled by referencing *every* assembly loaded in the Revit process (`AppDomain.GetAssemblies()`). On large/federated models that is hundreds of add-in/link DLLs, and `MetadataReference.CreateFromFile` over all of them exceeded the 60 s timeout — so *every* `send_code` call, even `return "ok";`, timed out. The fix references only the .NET shared framework (`TRUSTED_PLATFORM_ASSEMBLIES`) plus `RevitAPI`/`RevitAPIUI` plus `Newtonsoft` (`ExecuteCodeEventHandler.cs:120-141`), making compilation fast and deterministic and unblocking the executor's `execute_revit_api` fallback on real models.

### The LOCAL build recipe

All three `.csproj`s share the same offline recipe (`BimMcp.Sdk.csproj`, `BimMcp.Plugin.csproj`, `BimMcp.CommandSet.csproj`):

- **Target** — `net8.0-windows`, `x64`, `LangVersion 12`, `Nullable`/`ImplicitUsings` disabled, output partitioned per Revit version: `bin\$(Configuration)\$(RevitVersion)\` (`BimMcp.Sdk.csproj:5-11`).
- **Revit API via local HintPaths, `Private=False`** — `RevitAPI.dll`/`RevitAPIUI.dll` are referenced directly from `C:\Program Files\Autodesk\Revit $(RevitVersion)\`, with `<Private>False</Private>` so the multi-hundred-MB Revit assemblies are **not** copied into the output (they are already loaded in the Revit process) (`BimMcp.Sdk.csproj:16-27`). This is "the unblocking move" that replaced the `Nice3point`/`RevitMCPSDK` NuGet dependency.
- **Cached NuGet only** — `Newtonsoft.Json 13.0.3` is the sole `PackageReference`, because it is already in the local NuGet cache (`README.md:15`).
- **Vendored Roslyn** — the CommandSet references `Microsoft.CodeAnalysis.dll` and `Microsoft.CodeAnalysis.CSharp.dll` as **local DLLs from `..\..\libs\`** (`BimMcp.CommandSet.csproj:33-42`), not NuGet (NuGet is blocked), for `send_code_to_revit`'s compiler. The two DLLs are committed under `libs/` (~9 MB).
- **GlobalUsings** — `src/CommandSet/GlobalUsings.cs` declares the imports the fork previously got implicitly from `Nice3point.Build.Tasks` (System.*, `Autodesk.Revit.DB`/`UI`, Newtonsoft, `BimMcp.Sdk`, the common models). This is "what makes the bulk-ported commands compile without editing every file" (lines 1-16).
- **Cumulative `REVIT*_OR_GREATER` defines** — for `RevitVersion==2026`, all of `REVIT2020..2026_OR_GREATER` are defined; for `2025`, up to `REVIT2025_OR_GREATER` (`BimMcp.Sdk.csproj:12-13`). This lets version-gated Revit-API code compile the same way the Nice3point build did.
- **Plugin extras** — `UseWPF` (for the settings window), `EnableDynamicLoading`, `CopyLocalLockFileAssemblies`, a `ProjectReference` to the SDK, and `.addin` copied to output (`BimMcp.Plugin.csproj:10-39`).

`scripts/build.ps1` builds Plugin + CommandSet for `2025` and `2026` with `dotnet build -c Release -p:RevitVersion=$v` — no CI, no Revit needed to build (`build.ps1:1-13`).

### RevitDialogSuppressor — truly headless

`src/Plugin/Core/RevitDialogSuppressor.cs` (`dd05e79`) makes the socket server headless. Revit raises modal dialogs and transaction "failures" (e.g. "Room is not in a properly enclosed region") that block the UI thread — which freezes the server and times out the command (`RevitDialogSuppressor.cs:10-18`). It installs two global handlers once, at startup (`SocketService.cs:57`):

- **`DialogBoxShowing`** → `e.OverrideResult(1)` (IDOK) auto-accepts any modal so none is left open (lines 40-43).
- **`Application.FailuresProcessing`** → deletes warnings, resolves resolvable errors, and rolls back the rest: it iterates `FailureMessageAccessor`s, `DeleteWarning` for warnings, `ResolveFailure` for errors that `HasResolutions()`, and sets `ProceedWithRollBack` if any error is unresolvable, else `ProceedWithCommit` (lines 46-83).

Net effect: a command either succeeds or fails fast with a message — it never hangs on a popup. Both handlers swallow their own exceptions so the suppressor can never itself raise a dialog.

### Self-extension: grow / hot-load / repair

The self-extension infrastructure (`b00b0c4`, `c8a0848`, `44869e4`, `b219176`) lets an agent add a command to a running Revit **without a restart**:

- **`ExtensionLoader`** (`src/Plugin/Core/ExtensionLoader.cs`) loads "grown" DLLs from `Commands\Extensions\`. Each grown command is its *own* assembly referencing `BimMcp.Sdk`, so a freshly compiled command can be dropped in and hot-loaded without rebuilding the main command set (lines 11-19). It **must run on the UI thread** because instantiating an `ExternalEventCommandBase` creates an `ExternalEvent` (lines 15-19).
- **`reload_commands`** (`src/Plugin/Core/ReloadCommandsCommand.cs`) is itself an `ExternalEventCommandBase`, so its handler runs `ExtensionLoader.LoadExtensions` on the UI thread and reports `{ success, loaded, total }` (lines 25-72). It is "the linchpin of the grow loop: the Tool Engineer compiles a new command, drops the DLL in `Extensions\`, then calls this" (lines 10-15). 30 s timeout, guarded by a static lock.
- **The grow pipeline** (`tools/grow/`): `grow_command.workflow.js` orchestrates a **writer → reviewer → tester → repair** loop (up to 3 attempts) driven by an explicit SDK CONTRACT and a canonical read-only example (`grow_command.workflow.js:18-97, 124-151`). `build_command.py` compiles one `.cs` via the `GrownCommand.csproj` template against the *deployed* `BimMcp.Sdk.dll` and copies the DLL into `Commands\Extensions\` (`build_command.py:37-56`). `call.py` is a minimal JSON-RPC client for `:8080` used to smoke-test the new command (`call.py:10-24`). The loop was proven live in `44869e4` (2 commands grown + hot-loaded, no restart) and closed end-to-end in `b219176` (a captured `send_code` fallback promoted into a compiled tool, `set_view_scale`).

### Deployment: deploy.ps1 / replace.ps1

- **`scripts/deploy.ps1`** copies the built output to `%APPDATA%\Autodesk\Revit\Addins\<ver>\BimMcp\`: the Plugin host + deps into `BimMcp\`, the `.addin` manifest into the `Addins\<ver>\` root, and **all** CommandSet DLLs (including vendored Roslyn) + `commandRegistry.json` into `BimMcp\Commands\` (`deploy.ps1:14-31`). It **refuses to run while Revit is open**, because Revit file-locks the command DLL (lines 8-11) — matching the standing instruction that the assistant may close/relaunch Revit itself for a deploy. The "copy ALL DLLs" behavior was a deliberate fix in `fe5fc0b` (the earlier version copied only 4 named files and dropped the Roslyn dependency `send_code_to_revit` needs).
- **`scripts/replace.ps1`** (`4770306`) performs the cutover from the fork: it runs `deploy.ps1`, then **retires the fork** by renaming `mcp-servers-for-revit.addin` → `.addin.disabled` so only `bim-mcp` loads and owns `:8080` (`replace.ps1:13-24`). The rename (not delete) makes it reversible.

### Verified provenance

All claims above are read from the actual source and confirmed against git history. Key commits: `81445fa` scaffold; `e97b9e7` vendored SDK; `8a6e7d4` first local build (R2025+R2026, 0 errors); `369fcfd` full command port; `6d291e4` ribbon + `send_code_to_revit`; `dd05e79` `RevitDialogSuppressor`; `da32355` curated-references perf fix + `tag_element` fix; `b00b0c4`/`c8a0848`/`44869e4`/`b219176` the grow loop.

**Relevant files** (all under `C:/Users/DE1E7A/bim-mcp/`): `src/Sdk/JsonRpc/JsonRpcModels.cs`, `src/Sdk/Base/ExternalEventCommandBase.cs`, `src/Sdk/Base/RevitVersionAdapter.cs`, `src/Sdk/Interfaces/*.cs`, `src/Plugin/Core/{Application,SocketService,CommandManager,RevitCommandRegistry,RevitDialogSuppressor,ExtensionLoader,ReloadCommandsCommand,MCPServiceConnection,SettingsCommand}.cs`, `src/Plugin/Configuration/*.cs`, `src/Plugin/Utils/{PathManager,Logger}.cs`, `src/Plugin/UI/CommandSettingsWindow.cs`, `src/Plugin/BimMcp.addin`, `src/CommandSet/{GlobalUsings.cs,commandRegistry.json}`, `src/CommandSet/Models/Common/AIResult.cs`, `src/CommandSet/Commands/ExecuteDynamicCode/{ExecuteCodeCommand,ExecuteCodeEventHandler}.cs`, `src/CommandSet/Services/{DialogWatcher.cs,AnnotationComponents/TagElementEventHandler.cs}`, `scripts/{build,deploy,replace}.ps1`, `tools/grow/{grow_command.workflow.js,build_command.py,call.py,template/GrownCommand.csproj}`, and the three `.csproj` files.

## Self-extension — the grow / repair loop

The defining capability of the owned `bim-mcp` server (`C:/Users/DE1E7A/bim-mcp`) is that it can acquire *new* Revit commands at run time. A grown command is compiled to its own DLL and hot-loaded into the *running* Revit process — no rebuild of the main command set, no restart. Two complementary drivers feed this mechanism: a **Tool Engineer** that grows a command from a natural-language spec, and a **closed code→tool loop** that watches the personalization executor (`C:/Users/DE1E7A/revit-personalization`) write ad-hoc C#, then distills each successful snippet into a permanent compiled tool. Four commands have been grown to date: `get_levels`, `grown_command`, `room_area_by_level`, and `set_view_scale`.

This section documents the enabling infrastructure (hot-loading), the SDK contract every grown command must satisfy, the build toolchain, the Tool Engineer workflow, and the end-to-end code→tool loop, citing the actual sources and the commits that introduced them.

### The enabling mechanism: hot-loading grown DLLs

The infrastructure was introduced in commit `b00b0c490a686764c3220fccd755ad139e1346a4` ("self-extension infra: hot-load grown command DLLs (Extensions/ + reload_commands) — no restart to grow"). It rests on three plugin files plus one integration point.

**A dedicated extensions folder.** Grown DLLs live in a folder distinct from the built-in command set. `PathManager.GetExtensionsDirectoryPath()` resolves `<plugin dir>\Commands\Extensions\` and creates it on demand (`src/Plugin/Utils/PathManager.cs:34-39`), sitting beside the built-in `Commands\` directory (`PathManager.cs:19-24`). Because it lives under the deployed plugin, the deployed `BimMcp.Sdk.dll` and `Newtonsoft.Json.dll` are one directory up — the same instances the running Revit has already loaded, which is what makes reflection type-identity hold (see the SDK contract below).

**The loader.** `ExtensionLoader.LoadExtensions(registry, uiApp, logger)` enumerates every `*.dll` in the Extensions folder, `Assembly.LoadFrom`s it, and for each concrete type assignable to `IRevitCommand` instantiates it and calls `registry.RegisterCommand(cmd)` (`src/Plugin/Core/ExtensionLoader.cs:29-60`). It supports two construction conventions — a type implementing `IRevitCommandInitializable` is created then handed the live `UIApplication` via `Initialize(uiApp)`, otherwise a `(UIApplication)` constructor is used, falling back to a parameterless one (`ExtensionLoader.cs:40-51`). Every per-type and per-DLL failure is caught and logged, so one malformed DLL cannot abort the whole load (`ExtensionLoader.cs:56,59`). The class doc is explicit that it "MUST run on the UI thread" because instantiating an `ExternalEventCommandBase` calls `ExternalEvent.Create`, which is only valid in a UI-thread context (`ExtensionLoader.cs:11-19`).

**Two invocation sites.** `SocketService.Initialize` calls the loader once at startup (after the built-in `CommandManager.LoadCommands()`), then registers the hot-reload command itself (`src/Plugin/Core/SocketService.cs:59-63`):

```csharp
// Self-extension: load any grown command DLLs (Commands\Extensions\) + register hot-reload.
try { ExtensionLoader.LoadExtensions(_commandRegistry, _uiApp, _logger); }
catch (Exception ex) { _logger.Error("Extension load at startup: {0}", ex.Message); }
try { _commandRegistry.RegisterCommand(new ReloadCommandsCommand(_uiApp, _commandRegistry, _logger)); }
catch (Exception ex) { _logger.Error("register reload_commands: {0}", ex.Message); }
```

**Hot-reload without restart.** `reload_commands` is the linchpin. `ReloadCommandsCommand` is itself an `ExternalEventCommandBase` — its `Execute` locks, then `RaiseAndWaitForCompletion(30000)` (`src/Plugin/Core/ReloadCommandsCommand.cs:27-31`). The raise dispatches to `ReloadCommandsHandler.Execute(UIApplication app)`, which runs *on the UI thread* (the only context where `ExternalEvent.Create` in each freshly-instantiated grown command is legal) and calls `ExtensionLoader.LoadExtensions` again, re-scanning the folder and registering any DLLs added since startup (`ReloadCommandsCommand.cs:57-72`). It returns `{ success, loaded, total }` so the caller learns which command names were picked up. The socket thread never touches the Revit API directly — the raise→`WaitForCompletion`→`Set()` handshake marshals every load onto the UI thread. This is what lets the Tool Engineer drop a new DLL into `Extensions\` and make it live with a single JSON-RPC call.

| File | Role | Key symbol |
|------|------|-----------|
| `src/Plugin/Utils/PathManager.cs` | Resolves `Commands\Extensions\` | `GetExtensionsDirectoryPath()` (`:34`) |
| `src/Plugin/Core/ExtensionLoader.cs` | Reflection-loads + registers each DLL | `LoadExtensions()` (`:22`) |
| `src/Plugin/Core/ReloadCommandsCommand.cs` | UI-thread hot-reload command | `ReloadCommandsCommand` (`:16`), `ReloadCommandsHandler` (`:37`) |
| `src/Plugin/Core/SocketService.cs` | Startup load + registers `reload_commands` | `Initialize()` (`:59-63`) |

### The SDK contract a grown command must follow

Every grown command references *only* four assemblies — `BimMcp.Sdk`, `Autodesk.Revit.DB`, `Autodesk.Revit.UI`, and `Newtonsoft.Json` — and must implement the vendored SDK's minimal dispatch surface. The contract is dictated by four interfaces plus one base class in `src/Sdk/`:

- **`IRevitCommand`** (`src/Sdk/Interfaces/IRevitCommand.cs:10-18`) — the dispatch contract: a `string CommandName` (the JSON-RPC method name) and `object Execute(JObject parameters, string requestId)` returning a JSON-serializable result. `SocketService` + `CommandManager` discover implementations by reflection, which is why the grown assembly must bind to the *same* `BimMcp.Sdk` instance already in Revit.
- **`ExternalEventCommandBase`** (`src/Sdk/Base/ExternalEventCommandBase.cs:14-37`) — creates the `ExternalEvent` **once in the constructor** (`:24`) and exposes `RaiseAndWaitForCompletion(int ms)` which raises the event from the socket thread and blocks on the handler until the UI-thread work finishes (`:32-36`). The class comment calls this "the load-bearing correctness path — get it right or every command times out."
- **`IWaitableExternalEventHandler`** (`src/Sdk/Interfaces/IWaitableExternalEventHandler.cs:10-14`) — an `IExternalEventHandler` extended with `bool WaitForCompletion(int ms)`, the synchronous request/response handshake (concrete handlers back it with a `ManualResetEvent`).
- **`IRevitCommandInitializable`** (`src/Sdk/Interfaces/IRevitCommandInitializable.cs:9-12`) and **`ICommandRegistry`** (`src/Sdk/Interfaces/ICommandRegistry.cs:9-15`) — optional UI-app injection and the in-memory method→command map the loader registers into.

The full behavioural contract the writer/reviewer agents enforce is spelled out verbatim in `tools/grow/grow_command.workflow.js:18-97` and again (for the promotion path) in `tools/grow/promote_fallbacks.py:32-52`. In essence:

- `namespace BimMcp.Grown`, with **two classes**: a `Command : ExternalEventCommandBase` and a `Handler : IWaitableExternalEventHandler`.
- **All** Revit API access happens inside `Handler.Execute(UIApplication app)` (UI thread). `Command.Execute` only locks, copies inputs from `parameters` onto the handler, calls `RaiseAndWaitForCompletion`, and returns `handler.Result`.
- The handler owns a `ManualResetEvent`; `WaitForCompletion` does `Reset(); return WaitOne(ms)`; `Execute(app)` does the work in `try`, sets `Result`, and calls `_resetEvent.Set()` in a `finally` — **never throwing out of `Execute(app)`**.
- `Result` is an anonymous object: success `new { success = true, ... }`, failure `new { success = false, error = ex.Message }`.
- Read the model via `app.ActiveUIDocument.Document` with a null guard; use `ElementId.Value` (long) / `new ElementId(long)`; unit conversions `mm = feet*304.8`, `m² = ft²*0.092903`; any model change wrapped in `using (var t = new Transaction(doc, "...")) { t.Start(); …; t.Commit(); }`.
- **No `TaskDialog` / `MessageBox` / `Console`** — this is a headless socket server.

`tools/grow/examples/get_levels.cs` is the canonical read-only reference the writer agent mirrors; `set_view_scale.cs` (`tools/grow/grown/set_view_scale.cs`) shows the same shape with a parameter read (`Typed.Scale = parameters["scale"]?.Value<int>() ?? 100`, `:22`) and a transaction-wrapped write (`:51-56`); `room_area_by_level.cs` shows a non-trivial read-only aggregation faithful to the SDK shape.

### The build toolchain

Three artifacts under `tools/grow/` turn a `.cs` file into a live tool.

**`template/GrownCommand.csproj`** compiles exactly one `.cs` (`<EnableDefaultCompileItems>false</EnableDefaultCompileItems>` + explicit `<Compile Include="$(GrownSource)"/>`) targeting `net8.0-windows`, x64, into an assembly named `$(GrownName)` (`tools/grow/template/GrownCommand.csproj:7-22`). Critically, every reference — `BimMcp.Sdk`, `Newtonsoft.Json`, `RevitAPI`, `RevitAPIUI` — is `<Private>false</Private>` (`:25-28`): the grown DLL binds against the assemblies **already loaded in Revit** rather than shipping its own copies, so reflection type identity holds and the loaded `IRevitCommand` type matches the one the registry expects (`:2-5`). `SdkDll`, `NewtonsoftDll`, `RevitVersion`, `GrownName`, and `GrownSource` are all supplied as MSBuild properties by the builder.

**`build_command.py`** is the compiler front-end (`tools/grow/build_command.py`). It resolves the deployed `BimMcp.Sdk.dll` + `Newtonsoft.Json.dll` from `%APPDATA%\Autodesk\Revit\Addins\<ver>\BimMcp\Commands` (`:17-18`, `:31-36`), copies the template into an isolated temp dir, runs `dotnet build -c Release` with the five `-p:` properties (`:43-46`), and on success copies `<name>.dll` into the deployed `Commands\Extensions\` folder — the exact directory `reload_commands` scans — so the DLL is immediately hot-loadable (`:37-38`, `:54-56`). On failure it prints up to 30 lines containing "error" (`:49-52`), giving the repair loop something concrete to act on.

**`call.py`** is a minimal JSON-RPC client for the socket on `127.0.0.1:8080` (`tools/grow/call.py:10-24`) — the pipeline uses it to invoke `reload_commands` and then smoke-test the freshly grown command.

### The Tool Engineer: Writer → Reviewer → Tester → Repair

`tools/grow/grow_command.workflow.js` (commit `c8a0848f5819bc4359b4443fe5398bff4c317578`) is a four-phase agent workflow that grows a command from a spec:

1. **Write** (`grow_command.workflow.js:100-108`) — a writer agent receives the full `CONTRACT` plus the capability spec and writes the complete C# file to `tools/grow/grown/<name>.cs`, returning `test_params` for a smoke test.
2. **Review** (`:111-121`) — a reviewer agent re-reads the file and verifies contract conformance exactly (two classes; all Revit work on the UI thread; `_resetEvent.Set()` in `finally`; never throws out of `Execute(app)`; success/error `Result`; `CommandName` matches; transactions wrap writes; no `TaskDialog`/`Console`; correct `ElementId.Value`/unit conversions), **fixing the file in place** and returning `approved`.
3. **Test** (`:126-138`) — a tester agent runs, from the repo, `build_command.py`, then `call.py reload_commands "{}"`, then `call.py <name> '<test_params>'`, judging `ok=true` only if the build succeeded, `reload_commands` loaded the command (or it was already loaded), and the live call returned `success=true` (not an error or timeout).
4. **Repair** (`:141-150`) — on failure, the writer is re-invoked with the build error and test output and told to diagnose and fix the file; the Test↔Repair cycle runs up to **3 attempts** (`:125`, `:139-140`).

This workflow, run live, grew `room_area_by_level` and `grown_command` and hot-loaded both into a running Revit with no restart (commit `44869e4753f42567c907525f5af3d93f4a2877d4`, "grow loop PROVEN LIVE: Tool Engineer grew + hot-loaded 2 new commands").

### The closed code → tool loop

The second driver closes the loop between the two repositories automatically, turning the personalization executor's *ad-hoc* fallbacks into permanent tools. The stages (also summarized in `revit-personalization/PROJECT_STATUS.md:127-130`):

**1. The executor hits a capability gap and writes C#.** When no structured tool fits a step, the executor calls its last-resort `execute_revit_api` tool (`orchestrator/executor_agent.py:186-213`), whose body is run against the live model via the plugin's `send_code_to_revit` (`:719-737`). The tool is deliberately gated (last-resort framing, an `API_NUDGE` that forces a re-affirmation before the first raw-API drop, `:782-788`, and write-mode confirmation, `:757-762`).

**2. A capture hook records every *successful* fallback.** On success, `_record_capability_gap(code, args)` appends a `{ts, code, args}` record to `orchestrator/grow_candidates.jsonl` (`executor_agent.py:176-184`, invoked at `:733`). The comment names the intent precisely: "Every successful execute_revit_api use = a capability gap the agent had to fill with ad-hoc code" (`:170-172`). Both files are git-ignored (`revit-personalization/.gitignore:54-55`).

**3. `promote_fallbacks.py` distills → compiles → hot-loads → tests → registers.** `tools/grow/promote_fallbacks.py` is the "promotion Tool Engineer." For each unseen candidate (dedup by SHA-1 of the code, tracked in `tools/grow/.promoted.json`, `:150-167`) it:
   - asks the LLM to **distill** the raw snippet into a clean, *parameterized*, helper-decomposed `BimMcp.Grown` command plus an Anthropic `input_schema`, `description`, and `test_params` (`promote_one`, `:112-131`; `WRITER` prompt `:54-69`; the same SDK `CONTRACT` `:32-52`);
   - **compiles** it via `build_command.py` (`_build`, `:94-99`);
   - **hot-loads** it via `call.py reload_commands` and **live-tests** it via `call.py <name>` (`:123-127`);
   - on `success=true`, **registers** its schema into `orchestrator/grown_tools.json`, de-duplicating by name (`_register`, `:102-109`, `:128`);
   - self-repairs: a build or test failure is fed back to the writer for **one** retry (`:114`, `:120-121`, `:130`).

   A one-off mode (`--spec`/`--name`) grows a command directly from a description with no candidate queue (`:143-146`).

**4. `revit_tools` loads `grown_tools.json`; the executor advertises + dispatches it.** `orchestrator/revit_tools.py` reads `grown_tools.json` at import (`_load_grown`, `:89-102`, `:119`), skipping any name that is blocked or shadows a built-in command (`:95`). Grown schemas are appended to the advertised toolset — `TOOL_SCHEMAS = tool_schemas() + grown_schemas()` and `TOOL_NAMES = built-ins ∪ grown` (`:120-121`), refreshable at run time via `reload_grown()` (`:110-116`). The executor folds `revit_tools.TOOL_SCHEMAS` into the toolset it presents to the model (`executor_agent.py:289-293`), and `dispatch` routes a grown tool by its own command name, passing args straight through to the plugin (`revit_tools.py:142-152`). The next time the same capability is needed the model sees a **real, structured tool** and never re-authors code.

This full round-trip was proven in commit `b219176ff73e0892a35158f139d74d1ee6646b5a` ("grow loop closed end-to-end: promoted a captured code-fallback into a compiled tool (set_view_scale)"). The seed candidate is still visible in `grow_candidates.jsonl` — an ad-hoc `view.Scale = 50;` snippet whose `purpose` records that "the numeric scale is a View.Scale property only settable via the API. No structured tool can express this." It was distilled into the parameterized `set_view_scale.cs` (a `scale` param, 1–32767 validation, transaction-wrapped write) and registered in `grown_tools.json` as a first-class tool.

### What has been grown

| Command | Origin | Kind | Source |
|---------|--------|------|--------|
| `get_levels` | Canonical example / Tool Engineer seed | read-only (levels by elevation) | `tools/grow/examples/get_levels.cs` |
| `grown_command` | Tool Engineer (`grow_command.workflow.js`) | read-only model summary | `tools/grow/grown/grown_command.cs` |
| `room_area_by_level` | Tool Engineer (`grow_command.workflow.js`) | read-only room-area aggregation by level | `tools/grow/grown/room_area_by_level.cs` |
| `set_view_scale` | Closed code→tool loop (`promote_fallbacks.py`) | transactional write (active-view scale) | `tools/grow/grown/set_view_scale.cs`; registered in `revit-personalization/orchestrator/grown_tools.json` |

### Why this matters (thesis framing)

The two drivers are complementary. The **Tool Engineer** is *deliberate* growth — an operator (or an agent) names a capability and the writer/reviewer/tester/repair pipeline produces a vetted, hot-loaded command. The **closed code→tool loop** is *emergent* growth — the system observes its own capability gaps in the wild (the executor's ad-hoc C#), and asymptotically converges the tool library on "all the tools we need," each captured snippet becoming a clean, parameterized, compiled command exactly once (SHA-1 dedup). Both share the same load-bearing substrate: the vendored SDK contract, the single-file MSBuild template with non-private references, and the UI-thread `reload_commands` handshake that makes a new DLL live without a restart.

The principal correctness risk throughout is the UI-thread discipline of the Revit `ExternalEvent` model: `ExternalEvent.Create` must run in a UI context, all API access must occur inside `Handler.Execute(UIApplication)`, and the socket thread must only raise-and-wait. Every layer — the SDK base class, the contract embedded in both agent pipelines, and the reviewer phase — exists to enforce that invariant, because a violation does not fail loudly at compile time; it deadlocks or times out at run time. A committee-facing caveat: the loop's *safety* rests on the reviewer/tester gates and the executor's confirmation/undo model rather than on any formal sandbox — a grown command runs with full Revit API authority in-process, so the guarantees are procedural (review + transactional rollback + human confirmation on writes), not enforced isolation.

---

# Appendices

## Appendix A — Repositories & layout

| Repo | Role | Lang | Location | Remote / branch |
|---|---|---|---|---|
| **revit-personalization** | the brain (detect→understand→execute→chat) | Python | `C:/Users/DE1E7A/revit-personalization` | github.com/MohamedKMahdy/revit-personalization · `main` |
| **bim-mcp** | owned Revit MCP server (executes in Revit) | C# (.NET 8) | `C:/Users/DE1E7A/bim-mcp` | github.com/MohamedKMahdy/bim-mcp (private) · `master` |
| **generalBIMlog** | Revit add-in that logs user actions | C# | `C:/Users/DE1E7A/generalBIMlog` | `feature/cloud-sync` |
| **mcp-servers-for-revit** | **RETIRED** upstream fork (replaced by bim-mcp) | C# | `C:/Users/DE1E7A/mcp-servers-for-revit` | fork — `.addin.disabled` |

**revit-personalization** (35 modules): `orchestrator/` (executor_agent, revit_tools, pattern_agent, project_memory, understanding, rule_induction, compiled_skill), `detector/` (v2_cluster, v3_compound), `mcp_server/` (revit_bridge, generalbimlog_reader), `chatbot/` (chat_server), `predictor.py`, `pattern_watcher.py`, `shared/` (schemas, llm), `eval/`, `tests/` (212).

**bim-mcp** (162 C# files, 3 projects): `src/Sdk` → BimMcp.Sdk.dll; `src/Plugin` → BimMcp.Plugin.dll; `src/CommandSet` → BimMcp.CommandSet.dll; `tools/grow/` (self-extension toolchain); `scripts/` (build/deploy/replace).

## Appendix B — Operational facts

- **Ports:** chatbot **:5000** (FastAPI/uvicorn); bim-mcp plugin **:8080** (JSON‑RPC over TCP, inside Revit).
- **Run the chatbot:** `pythonw chatbot/chat_server.py --no-browser` (cwd = repo). Confirm `http://127.0.0.1:5000/` → 200. **Restart after any backend edit.**
- **Build + deploy bim-mcp:** `scripts/build.ps1`, then `scripts/deploy.ps1` (Revit must be **closed** — it locks the DLLs). `scripts/replace.ps1` cuts over to :8080 and retires the fork.
- **Deployed path:** `%APPDATA%\Autodesk\Revit\Addins\<2025|2026>\BimMcp\` (commands + `Extensions\` + `commandRegistry.json`, port 8080).
- **Models / cost:** ceiling = Sonnet (`EXECUTOR_MODEL`), cheap = Haiku (`EXECUTOR_CHEAP_MODEL`); simple/warm routines start Haiku, escalate on difficulty; compiled replay = **$0**. Steady‑state placement ≈ **$0.03 or free**.
- **Logs (per‑user):** `%LOCALAPPDATA%\RevitPersonalization\logs\` — `executor_runs.jsonl` (per‑run `est_cost_usd` + usage), `executor_transcripts.jsonl` (full reasoning + tool args), `understanding_ledger.jsonl`.
- **send_code contract:** variable is `document` (Document), code is the body of `object Execute(Document document, object[] parameters)`, must `return`.
- **Cost knobs (env):** `EXECUTOR_ADAPTIVE`, `EXECUTOR_ESCALATE_AFTER_FAILURES`, `EXECUTOR_READ_LOOP_CAP`, `EXECUTOR_ALLOW_API_FALLBACK`, `EXECUTOR_MAX_ITERS`.

## Appendix C — Known issues & limitations

- **Routine portability across models.** A routine learned in model A names a family that may not exist in model B. Mitigated — `place_element` substitutes the closest loaded family or lists + asks — but the routine still *names* the original family.
- **Correction format on a fresh model.** Corrections carry forward because a corrected value becomes a real element (the live model is the feedback). A corrected *format* on a brand‑new/empty model is not yet fed into the deterministic resolver (only surfaced as LLM prose).
- **Promotion gate.** Grown tools live in `Extensions/` (durable; reload at startup) but are not yet auto‑folded into the **core** CommandSet.
- **Workflow `args`** do not bind to the script global in this runtime (worked around via script defaults).
- **Evaluation** is on synthetic/self data; a live n=3–6 user study is a flag‑flip away but not yet run.
- The retired fork clone is **kept** at `C:/Users/DE1E7A/mcp-servers-for-revit` until bim-mcp is fully trusted.

## Appendix D — Roadmap / what's left

1. **Verify live** — place a door in chat and confirm `TU 235`, tag offset, family list/substitute, Haiku cost (all three verified programmatically on 2026‑06‑30).
2. **Promotion gate** — fold a proven grown tool (e.g. `set_view_scale`) into the core CommandSet.
3. **Deliberate self‑repair demo** — break a command, watch the Tool Engineer fix it.
4. **Correction‑format feedback** into `resolve_routine_values` (survives an empty model).
5. **WARM‑vs‑COLD + foreign‑prior** meta‑learning experiment (results‑chapter evidence).
6. Delete the retired fork clone once fully confident.

## Appendix E — Glossary

- **Motif / MotifStep** — the learned, structured representation of a routine (ordered steps, params, intent, trigger); `shared/schemas.py`.
- **CandidateRoutine** — a detector output (a cluster of repeated instances) with `support` + `confidence`.
- **Executor** — the agentic tool‑use loop (`run_executor`) that carries out a routine in the live model, self‑healing.
- **resolve_routine_values** — decides each parameter value at execution time; variable params (Mark) are anchored to the live model's convention.
- **Compiled skill** — a distilled, deterministic replay of a successful routine trace ($0, no LLM).
- **Per‑user prior / project memory** — the accumulating record (executions, last values, compiled skills, confirmed/corrected understanding) that personalizes behavior.
- **Understanding ledger** — an append‑only audit of what the system inferred and how the user confirmed/corrected it.
- **bim-mcp** — the student's own Revit MCP server (C# plugin) on :8080; replaced the mcp-servers-for-revit fork.
- **Grown command / tool** — a Revit command compiled into its own DLL and hot‑loaded at runtime (no restart) from `Extensions/`.
- **Tool Engineer** — the Writer→Reviewer→Tester→Repair agent pipeline that authors, compiles and verifies a new command.
- **execute_revit_api / send_code_to_revit** — the gated last‑resort path where the agent writes C# against the live Document; captured and later promoted into a compiled tool.
- **RevitDialogSuppressor** — the global handler that auto‑dismisses dialogs and auto‑resolves transaction failures so the headless server never blocks.

---

*End of document. Generated 2026‑06‑30 from the live source of both repositories.*
