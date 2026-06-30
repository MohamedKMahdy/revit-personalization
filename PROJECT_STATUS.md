# BIM Personalization — Project Status

_Master's thesis: **"Meta Learning for Realtime Behavioral Personalization"** (a.k.a. Agent-Augmented BIM Log Mining for Personalized Action Generation)._
_Status date: **2026-06-30**. Owner: Mohamed K. Mahdy._

---

## 1. Executive summary

The system **learns a user's repeated Revit workflow from logs, then helps them execute it faster** in their live model — and it now **improves itself as it is used** (it turns ad‑hoc code the agent writes into permanent compiled tools). Two production pieces are live and verified:

- **`revit-personalization`** — the Python "brain": log mining → routine detection → behavioral understanding → an agentic executor → a chatbot UI. Runs on :5000. **212 tests.**
- **`bim-mcp`** — the student's **own** Revit MCP server (C# plugin in Revit), which **replaced** the upstream fork on **:8080**. **34 core commands + 4 grown** = self-extending. Builds locally; backed up on GitHub.

Everything described here is committed + pushed in both repos and running.

---

## 2. Research framing (defense-relevant)

| Claim in the title | Where it lives in the system |
|---|---|
| **Personalization** | per‑user project memory + a routine learned from *that* user's logs |
| **Behavioral (understand, not just detect)** | rule induction (sequence/conditional/per‑context) + intent/trigger + active confirmation + an understanding ledger |
| **Meta‑learning** | in‑context few‑shot + an **accumulating per‑user prior**; corrections generalize to all future actions (not MAML/outer‑loop) — defensible as in‑context learning‑to‑learn (Brown 2020) |
| **Realtime** | proactive next‑action prediction; live execution in the model |
| **Self‑extending tool library** | the grow/repair loop = Voyager‑style skill library (Wang 2023), made concrete with **compiled** tools |

Honest boundaries to state in the defense: per‑user/local only (no cross‑user/team/cloud sharing yet); the meta‑learning is in‑context + accumulating prior, **not** gradient meta‑learning; evaluation is on synthetic/self data, drop‑in upgradeable to an n=3–6 user study.

---

## 3. System architecture (data flow)

```
 Revit (user works)                        Revit (assistant acts)
   │                                          ▲
   │ ① logs actions                           │ ⑦ JSON-RPC :8080
   ▼                                          │
 generalBIMlog (C# add-in)            bim-mcp plugin (C#, in Revit)
   │  event logs (.jsonl)               (34 core + grown commands)
   ▼                                          ▲
 detector/  ──②──► pattern_agent/ ──③──► Motif / CandidateRoutine
 (cluster repeats)   (learn a rich motif:        │
                      steps, params, intent)     │ ④ resolve values (Mark, etc.)
                                                 ▼      anchored to the LIVE model
 project_memory  ◄──⑥ learn from run──  orchestrator/executor_agent.py
 (per-user prior,    understanding)      (agentic tool-use loop, self-healing)
  compiled skills)                              │ ⑤ build_goal + run_executor
        ▲                                       ▼
        └──────────────  chatbot/chat_server.py (:5000, SSE) ── user chat UI
                          predictor.py (proactive next-action)
```

1. **generalBIMlog** logs the user's Revit actions to `.jsonl`.
2. **detector/** clusters repeated action sequences into candidate routines.
3. **pattern_agent/** (LLM) learns a rich **Motif**: ordered steps, parameters (constant vs variable), intent, trigger.
4. At execution, **`resolve_routine_values`** decides each parameter value — variable params (e.g. `Mark`) are **anchored to the live model's actual convention**.
5. **`build_goal` + `run_executor`** drive the agentic loop: place → set params → tag, self‑correcting, against **bim-mcp** on :8080.
6. **project_memory** records the run (executions, last values, compiled skill, confirmed understanding) — the accumulating per‑user prior.
7. **bim-mcp** executes each tool call in Revit and returns a result.

---

## 4. Repositories

| Repo | Role | Lang | Location | Remote / branch |
|---|---|---|---|---|
| **revit-personalization** | the brain (detect→understand→execute→chat) | Python | `C:/Users/DE1E7A/revit-personalization` | github.com/MohamedKMahdy/revit-personalization · `main` |
| **bim-mcp** | owned Revit MCP server (executes in Revit) | C# (.NET 8) | `C:/Users/DE1E7A/bim-mcp` | github.com/MohamedKMahdy/bim-mcp (private) · `master` |
| **generalBIMlog** | Revit add-in that logs user actions | C# | `C:/Users/DE1E7A/generalBIMlog` | `feature/cloud-sync` |
| **mcp-servers-for-revit** | **RETIRED** upstream fork (replaced by bim-mcp) | C# | `C:/Users/DE1E7A/mcp-servers-for-revit` | fork `feature/tier1-3-tools` — `.addin.disabled` |

### revit-personalization layout (35 modules)
- `orchestrator/` — `executor_agent.py` (the agentic loop + tool schemas + dispatch), `revit_tools.py` (full plugin tool surface + grown-tool loading), `pattern_agent.py`, `project_memory.py`, `understanding.py`, `rule_induction.py`.
- `detector/` — `v2_cluster.py` (+ `v3_compound.py` for multi-element routines).
- `mcp_server/` — `revit_bridge.py` (TCP client to :8080), `generalbimlog_reader.py`.
- `chatbot/` — `chat_server.py` (FastAPI :5000, SSE, the UI).
- `predictor.py`, `pattern_watcher.py`, `shared/` (schemas, llm), `eval/`, `tests/` (212 tests).

### bim-mcp layout (162 C# files, 3 projects)
- `src/Sdk/` → **BimMcp.Sdk.dll** — vendored minimal SDK (IRevitCommand, ExternalEventCommandBase, JSON-RPC POCOs). Its own assembly so reflection-based command discovery keeps one type identity.
- `src/Plugin/` → **BimMcp.Plugin.dll** — `IExternalApplication`: SocketService (:8080), CommandManager, RevitCommandRegistry, **RevitDialogSuppressor**, **ExtensionLoader + reload_commands**, ConfigurationManager, ribbon (Switch + Settings), `CommandSettingsWindow` (WPF).
- `src/CommandSet/` → **BimMcp.CommandSet.dll** — 34 commands (ported from the fork + `get_warnings` + `send_code_to_revit`).
- `tools/grow/` — the self-extension toolchain (template csproj, `build_command.py`, `call.py`, `promote_fallbacks.py`, `grow_command.workflow.js`).
- `scripts/` — `build.ps1`, `deploy.ps1`, `replace.ps1`.

---

## 5. Component status

| Component | Status | Notes |
|---|---|---|
| Logger (generalBIMlog) | ✅ working | feeds `.jsonl`; level extraction added |
| Detector (cluster + compound) | ✅ working | mines repeated + multi-element routines |
| Pattern Agent | ✅ working | learns rich motif (steps/params/intent/trigger) with a downgrade guard |
| **Understanding layer** | ✅ working | rule induction, intent/trigger, active confirmation, reflection, ledger |
| Project memory (per-user prior) | ✅ working | executions, last values, **compiled skills**, confirmed understanding |
| **Executor** (agentic loop) | ✅ working + hardened | self-healing, completion-enforced, prompt-cached, **runs on bim-mcp :8080** |
| Predictor (proactive) | ✅ working | next-action prediction |
| Chatbot (:5000) | ✅ live (HTTP 200) | SSE UI, pattern history, memory panel |
| **bim-mcp server** (:8080) | ✅ live, 34/34 verified | replaced the fork; clean, headless, builds locally |
| **Grow/repair loop** | ✅ proven live | captures fallback code → compiles → hot-loads a real tool |

---

## 6. bim-mcp owned server (detailed)

- **Replaced** the upstream fork: owns **:8080**, fork renamed `mcp-servers-for-revit.addin.disabled` (reversible).
- **34/34 commands verified live** on a real model (reads, create grid/level/wall/point/floor/room/framing/dimensions, place_and_configure, duplicate, set/operate/color/tag×3, delete, execute_transaction_group, export_view_image, **send_code_to_revit**, pick_point).
- **Builds locally** — `dotnet build -p:RevitVersion=2025|2026` using Revit API HintPaths + cached Newtonsoft + **vendored Roslyn**; no blocked NuGet, no CI.
- **Headless** — `RevitDialogSuppressor` auto-dismisses dialogs + auto-resolves transaction failures (DeleteWarning / ResolveFailure / RollBack) so nothing ever blocks the socket.
- **Visible in Revit** — "MCP Server (bim-mcp)" ribbon panel (Switch + Settings); Settings opens a command enable/disable window writing `commandRegistry.json`.
- **Clean** — all Chinese text and all `TaskDialog.Show` popups removed from the ported code.
- `send_code_to_revit` contract: variable is **`document`**, code is the body of `object Execute(Document document, object[] parameters)`, must `return`. Compile references only the .NET runtime + Revit + Newtonsoft (fixed the 60s timeout on large federated models).

---

## 7. Self-extension (grow/repair) loop — the headline

**Mechanism (proven live):**
- Each "grown" command is its **own DLL** in `…/BimMcp/Commands/Extensions/`, referencing `BimMcp.Sdk`.
- `ExtensionLoader` loads them at startup; **`reload_commands`** hot-loads new ones into a **running** Revit (no restart) via a UI-thread ExternalEvent.
- **Tool Engineer** (`grow_command.workflow.js`) = Writer → Reviewer → Tester → 3-round Repair: generates the C#, reviews it, compiles, hot-loads, live-tests.

**The closed loop (the "by time we'll have all the tools" vision), proven end-to-end:**
1. The executor hits a missing capability → writes ad-hoc C# via `execute_revit_api`.
2. A capture hook logs it to `orchestrator/grow_candidates.jsonl`.
3. `tools/grow/promote_fallbacks.py` distills it into a clean, **parameterized, compiled** command + an Anthropic tool schema, compiles + hot-loads + live-tests it, and registers it in `orchestrator/grown_tools.json`.
4. `revit_tools` loads `grown_tools.json` → the executor **advertises + dispatches** the new tool next time instead of re-writing code.

**Grown so far (deployed in `Extensions/`):** `get_levels`, `grown_command` (model stats), `room_area_by_level`, **`set_view_scale`** (the first one promoted from a genuine captured fallback).

---

## 8. This session's changelog (commits)

### bim-mcp (`master`)
| Commit | What |
|---|---|
| `369fcfd`→`6d291e4` | port full 33-command surface + ribbon + send_code (vendored Roslyn) |
| `4770306` | `replace.ps1` — cut over to :8080, retire the fork |
| `b6d5b65` | remove ALL Chinese + ALL TaskDialog.Show |
| `b21aafc` | Command Settings window + ribbon button |
| `dd05e79` | **RevitDialogSuppressor** (truly headless) |
| `da32355` | **send_code perf fix** + tag_element tag-family lookup |
| `b00b0c4` | **hot-load infra** (Extensions/ + reload_commands) |
| `c8a0848`→`44869e4` | **Tool Engineer** workflow + proven live (2 grown commands) |
| `b219176` | **grow loop closed** — promoted a captured fallback into `set_view_scale` |

### revit-personalization (`main`)
| Commit | What |
|---|---|
| `33ac8e5` | **Mark anchors to the live model** (corrections carry forward) + real tag offset |
| `20bc979` | hard guard against per-element read loops + batch rule |
| `b087761` | **`get_parameters_bulk`** — audit a category in ONE call (not 68) |
| `55bc2f4` | duplicate-Mark fix — read ALL marks via the bulk path (was a capped 50 sample) |
| `5b37656` | **place_element honest success + family substitution** (no thrash on a not-loaded family) |
| `3d8aad8` | **cost**: simple routines start on Haiku; escalate only on real difficulty |

---

## 9. Key operational facts

- **Ports:** chatbot **:5000** (FastAPI/uvicorn), bim-mcp plugin **:8080** (JSON-RPC over TCP, inside Revit).
- **Run the chatbot:** `pythonw chatbot/chat_server.py --no-browser` (cwd = repo). Confirm `http://127.0.0.1:5000/` → 200. **Restart after any backend edit.**
- **Build + deploy bim-mcp:** `scripts/build.ps1` then `scripts/deploy.ps1` (Revit must be **closed** — it locks the DLLs). `replace.ps1` cuts over + retires the fork.
- **Deployed path:** `%APPDATA%\Autodesk\Revit\Addins\<2025|2026>\BimMcp\` (commands + `Extensions\` + `commandRegistry.json`, port 8080).
- **Models / cost:** ceiling = Sonnet (`EXECUTOR_MODEL`), cheap = Haiku; simple/warm routines start Haiku, escalate on difficulty; compiled replay = **$0**. Steady-state placement ≈ **$0.03 or free**.
- **Logs (per-user):** `%LOCALAPPDATA%\RevitPersonalization\logs\` — `executor_runs.jsonl` (per-run `est_cost_usd` + usage), `executor_transcripts.jsonl` (full reasoning + tool args), `understanding_ledger.jsonl`.
- **Tests:** revit-personalization **212**; bim-mcp builds 0 errors both versions.

---

## 10. Known issues & limitations

- **Routine portability across models.** A routine learned in model A replays a family that may not exist in model B. Mitigated (`place_element` now substitutes the closest loaded family or lists + asks), but the routine still *names* the original family.
- **Correction format on a fresh model.** Corrections carry forward because they become real elements (the live model is the feedback). A corrected *format* on a brand-new/empty model isn't fed back into the deterministic resolver yet (only as LLM prose). Optional follow-up.
- **Promotion gate.** Grown tools live in `Extensions/` (durable, reload at startup) but aren't yet auto-folded into the **core** CommandSet.
- **Workflow `args`** don't bind in this runtime (worked around via script defaults).
- **Evaluation** is synthetic/self for now (no live n=3–6 user study yet).
- Retired fork clone is **kept** at `C:/Users/DE1E7A/mcp-servers-for-revit` until bim-mcp is fully trusted.

---

## 11. Roadmap / what's left

**Verify live (needs a model open):** place a door in chat → confirm `TU 235`, tag offset, family list/substitute, Haiku cost. _(All three verified programmatically against the live model on 2026‑06‑30.)_

**Optional, ready to build:**
1. Promotion gate — fold a proven grown tool (e.g. `set_view_scale`) into the core CommandSet.
2. Deliberate self‑repair demo — break a command, watch the Tool Engineer fix it.
3. Correction‑format feedback into `resolve_routine_values` (survives an empty model).

**Thesis / housekeeping:**
- WARM‑vs‑COLD + foreign‑prior negative‑control meta‑learning experiment (results‑chapter evidence).
- Delete the retired fork clone once fully confident.

---

## 12. Bottom line

A working, end‑to‑end personalization assistant: it **learns** a user's routine, **understands** it (rules + intent, confirmed + corrected), **executes** it in the live model through the student's **own** clean MCP server, **learns from corrections** (Mark now follows the project's real convention), runs **cheap** (Haiku/compiled), and **grows its own compiled tools** from the code it writes. All committed, pushed, and live.
