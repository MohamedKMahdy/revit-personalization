# Agent-Augmented BIM Log Mining for Personalized Action Generation

**Master's thesis system — full project documentation.**
Last updated: 2026-06-13. Keep this file as the single source of truth for the
project's architecture, file map, run instructions, and design decisions.

---

## 1. What this project is

A local system that watches a Revit user's repetitive modeling actions, detects
recurring routines ("Custom Element Instantiation" — e.g. *place door → set 4
parameters → tag*), and uses an LLM multi-agent pipeline to turn those routines
into one-click shortcuts — **all locally**, without uploading BIM logs to the
cloud.

The thesis contribution has three parts:
1. **Observation** — a C# Revit add-in logs the live action stream.
2. **Detection** — a Python detector mines repeated routines from the logs.
3. **Generation** — a two-agent Claude pipeline generalises a routine into a
   motif and replays it as MCP tool calls against the live model.

### Why both a C# add-in *and* an MCP server are needed
- The **Revit MCP server** is a *query/execute* interface — you ask it to do
  things. It does **not** stream real-time user actions; there is no MCP event
  for "user just placed a door." Only the Revit API event system
  (`DocumentChanged`) can observe the live action stream → requires a native C#
  add-in.
- When a shortcut runs, the Python side bridges to the **mcp-servers-for-revit**
  backend to actually execute `place_element` / `set_parameter` / `tag_element`,
  so we don't re-implement Revit write logic.

---

## 2. Repositories & local locations

| Repo | Local path | Purpose | Git |
|---|---|---|---|
| **revit-personalization** | `C:\Users\DE1E7A\revit-personalization` | The thesis system: C# logger, Python detector, MCP server, orchestrator, chatbot | git repo; detector work currently uncommitted (last commit `13cc5ee`) |
| **mcp-servers-for-revit** | `C:\Users\DE1E7A\mcp-servers-for-revit` | Open-source Revit MCP execution backend, extended with 10 new tools | branch `feature/tier1-3-tools` on top of upstream `86cf705`; not yet pushed to a fork |

### Data / history file locations (outside the repos)
| What | Path |
|---|---|
| **Action logs** (C# logger output, detector input) | `C:\Users\DE1E7A\AppData\Local\RevitPersonalization\logs\session_*.jsonl` |
| Add-in diagnostic log | `…\RevitPersonalization\logs\_diag.txt` |
| **Shortcuts** (generated configs) | `C:\Users\DE1E7A\AppData\Local\RevitPersonalization\shortcuts\` |
| Env / API key | `…\RevitPersonalization\.env` (gitignored) and project `.env` |
| Synthetic test logs (pre-grouped) | `revit-personalization\tests\synthetic_logs\*.json` |

> **Security constraint:** the Anthropic API key must NEVER be committed. It lives
> only in `.env` files (gitignored) and `%LOCALAPPDATA%\RevitPersonalization\.env`.

---

## 3. End-to-end architecture

```
Revit 2025/2026/2027
  │
  ├── C# Add-in: RevitLogger  (OBSERVER ONLY — never writes the model)
  │     • subscribes to Application.DocumentChanged
  │     • emits one ActionRecord per action → session_*.jsonl
  │     • RoutineDetector (C#) fires a live in-session suggestion
  │     • NotificationUI.xaml → "Learn this routine?" WPF prompt
  │
  └── C# Add-in/Plugin: mcp-servers-for-revit plugin  (EXECUTOR)
        • TCP/JSON-RPC server on localhost:8080
        • runs predefined tool commands on the Revit UI thread

Local machine (Python)
  │
  ├── detector/                 ← routine detection gate (v0.1 baseline + v0.2 default)
  │
  ├── mcp_server/  (FastMCP)    ← exposes logs as resources + analysis tools
  │     • resource logs://candidate_routines
  │     • resource logs://routine/{id}/examples
  │     • tools analyze_pattern, generate_command, execute_revit_command
  │     • revit_bridge.py → bridges to mcp-servers-for-revit (localhost:8080)
  │
  ├── orchestrator/  (Anthropic SDK)
  │     • pattern_agent.py  — examples → generalized motif JSON   (Opus)
  │     • macro_agent.py    — motif → MCP tool call sequence      (Sonnet)
  │
  └── chatbot/  (FastAPI, localhost:5000)
        • chat_server.py — conversational UI, SSE streaming, ##TOKEN## controls

TypeScript MCP server (mcp-servers-for-revit/server)
  • talks MCP to Claude, forwards tool calls over TCP to the in-Revit plugin
```

**The two MCP languages explained:** the MCP protocol's official SDK is
TypeScript, so the *server that talks to Claude* is TS. The *Revit API* is
C#-only and must run inside Revit, so the *executor* is a C# plugin. They are
joined by a TCP socket (localhost:8080, JSON-RPC).

---

## 4. revit-personalization — file map

```
revit-personalization/
├── PROJECT_OVERVIEW.md          ← this file
├── README.md, METHODOLOGY.md    ← thesis write-up
├── requirements.txt             ← mcp, anthropic, httpx, pydantic, pytest
├── docs/                        ← architecture, compliance, plugin notes
│
├── shared/
│   └── schemas.py               ← ActionRecord, CandidateRoutine, Motif, ShortcutConfig (pydantic)
│
├── revit_addin/                 ← C# add-in (OBSERVER). .NET 8, Revit 2025–2027
│   ├── ActionRecord.cs          ← C# DTO mirroring shared/schemas.py (snake_case JSON)
│   ├── ActionCapture.cs         ← DocumentChanged subscription, builds ActionRecords
│   ├── RoutineDetector.cs       ← in-session live detector → fires suggestion
│   ├── LogWriter.cs             ← async JSONL writer
│   ├── PatternBridge.cs         ← on a repeat, launches the Python chatbot (:5000)
│   ├── ShortcutRunner.cs        ← retired stub (execution → mcp-servers-for-revit)
│   ├── README.md                ← role, build/deploy, known gaps
│   └── App.cs, SessionInfo.cs, ElementSnapshot.cs
│
├── detector/                    ← ★ routine detection gate (see §6)
│   ├── base.py                  ← DetectorConfig + Detector protocol
│   ├── _common.py               ← key derivation, tokenize, Levenshtein, Jaccard, formatters
│   ├── v2_cluster.py            ← ClusterDetector (DEFAULT)
│   ├── v1_substring.py          ← SubstringDetector (baseline, comparison only)
│   ├── synthetic.py             ← synthetic-log generator (5 scenarios)
│   ├── __init__.py              ← make_detector() factory, default "v2"
│   └── README.md
│
├── mcp_server/
│   ├── server.py                ← FastMCP: resources + tools
│   ├── log_reader.py            ← parses JSONL, runs selected detector (v0.2 default)
│   └── revit_bridge.py          ← HTTP/TCP client → mcp-servers-for-revit
│
├── orchestrator/
│   ├── agents.py                ← coordinates the two agents
│   ├── pattern_agent.py         ← examples → motif      (claude-opus-4-x, adaptive thinking)
│   └── macro_agent.py           ← motif → tool sequence (claude-sonnet-4-6)
│
├── chatbot/
│   └── chat_server.py           ← FastAPI conversational UI (localhost:5000)
│
├── RevitWriteServer/            ← legacy C# TCP write server (superseded by mcp-servers-for-revit)
│
├── eval/
│   └── run_experiment.py        ← Pattern Agent quality vs. k examples (§4.4 metrics)
│
├── tests/
│   ├── test_detector.py         ← 12 tests for the v0.2 detector + v0.1 contrast
│   └── synthetic_logs/*.json    ← pre-grouped CandidateRoutine fixtures
│
├── check_logs.py                ← diagnostic: dump what the add-in wrote
├── test_mcp_server.py           ← script: end-to-end MCP server check
└── setup_revit_env.py           ← writes the %LOCALAPPDATA% .env
```

---

## 5. The action log schema (`ActionRecord`)

Defined in `shared/schemas.py` (pydantic) and mirrored in
`revit_addin/ActionRecord.cs` (C#). `schema_version: "2.0"`, snake_case JSON keys,
one object per line in `session_*.jsonl`. Follows the Jang & Lee (2023) enhanced
BIM-logging lexicon.

Key fields for detection:

| Field | Type | Notes |
|---|---|---|
| `action_type` | str | `Place` \| `SetParam` \| `Tag` \| `Delete` |
| `element_id` | int | for **Tag** this is the tag's own id; labeled element is `tagged_element_id` |
| `timestamp_unix` | float | also `timestamp_utc` |
| `element_category` | str | e.g. `Doors` |
| `family_name`, `type_name` | str | Place |
| `param_name`, `param_value_before/after`, `param_group` | – | SetParam |
| `tag_family_name`, `tagged_element_id` | – | Tag |
| `view_id/name/type`, `level_name`, `phase_name` | – | context |
| `location_x/y/z`, `host_id`, `flip_facing/hand` | – | geometry (Place) |

**`key`** is *not* a stored field but is derived in the Python featurizer
(no C# change needed): Place→`family_name`, SetParam→`param_name`,
Tag→`tag_family_name`.

Other models: `CandidateRoutine` (carries `support`=frequency and
`confidence`=tightness, plus `examples`), `Motif`/`MotifStep` (Pattern Agent
output), `ShortcutConfig` (saved one-click shortcut).

---

## 6. Routine detection (`detector/`)

Detection gate only — deterministic, no Revit calls, no model writes. Two
versions behind one `Detector` protocol; **v0.2 is the default**, v0.1 is kept
explicitly selectable for the precision/recall comparison.

| Version | Class | Algorithm |
|---|---|---|
| **v0.2** (default) | `ClusterDetector` | tokenize → segment → featurize → cluster → threshold → cooldown |
| **v0.1** (baseline) | `SubstringDetector` | collapse each action to P/S/T/D char, group contiguous Place-delimited episodes by exact shape |

### v0.2 pipeline
1. **Tokenize** — each record → `"{action}:{key}"`.
2. **Segment** — open an instance at each Place; append SetParam by `element_id`
   and Tag by `tagged_element_id`; close on the next Place for that element or an
   idle gap. Idle gaps bump a session counter. Discard instances shorter than
   `min_instance_tokens`.
3. **Featurize** — per instance: ordered token sequence + flat feature set
   `{fam:…, param:…, tag:…}`.
4. **Cluster** — greedy average-linkage at threshold `theta`, similarity =
   `w_set·Jaccard(featureset) + w_seq·(1 − normEdit(sequence))`.
5. **Threshold** — clusters with ≥ `N` members emit a `CandidateRoutine`.
6. **Cooldown** — a signature surfaced within `T` minutes (data time) is
   suppressed; the existing cluster is grown instead.

### Parameters (`DetectorConfig`)
| Field | Default | Meaning |
|---|---|---|
| `min_cluster_size` (N) | 3 | min members to emit |
| `theta` | 0.80 | grouping similarity threshold |
| `cooldown_minutes` (T) | 10 | suppress re-emit window |
| `min_instance_tokens` | 3 | discard shorter instances |
| `idle_gap_minutes` | 5 | gap that closes instances / splits sessions |
| `w_set` / `w_seq` | 0.6 / 0.4 | feature-set vs. sequence weight |

### Two ranking axes on `CandidateRoutine`
- **`support`** (= `count`) — cluster size = **frequency** signal.
- **`confidence`** — grouping quality, but **meaning differs by detector and the
  two are NOT comparable**:
  - v0.2 → mean pairwise intra-cluster similarity (tightness, 0–1).
  - v0.1 → legacy frequency-based `min(1, count/5)`.
  → The v0.1-vs-v0.2 evaluation must compare on **detection precision/recall**
  against the labeled session, not on the confidence value.

### Why v0.2 replaces v0.1 (four fixed weaknesses)
1. param/family blind (all SetParams collapse to `S`) → tokens carry keys.
2. contiguity required (misses interleaved repeats) → segment by id, not position.
3. exact equality (splits 3-vs-4 param variants) → edit distance + Jaccard.
4. mines arbitrary noisy substrings → only Place-rooted instances.

### Selecting the detector
```python
list_candidate_routines()                 # default v0.2
list_candidate_routines(detector="v1")    # baseline
# or environment:  REVIT_DETECTOR_VERSION=v1
```

---

## 7. mcp-servers-for-revit — the extension

The open-source backend that actually executes Revit operations. Architecture:
TypeScript MCP server (`server/src/tools/*.ts`, auto-registered by `register.ts`)
+ C# plugin (TCP server) + C# commandset (`Services/*EventHandler.cs` +
`Commands/*Command.cs`). All writes run on the Revit UI thread via the
`IExternalEventHandler` + `RaiseAndWaitForCompletion` pattern.

### 10 tools added (branch `feature/tier1-3-tools`)
Each is TS + C# (EventHandler + Command), except `resolve_category` (TS-only).

| Tier | Tool | What it does |
|---|---|---|
| 1 | `set_element_parameter` | set/batch-set parameters on an element |
| 1 | `get_element_parameters` | read params (name, value, storageType, isReadOnly) |
| 1 | `tag_element` | tag one element; auto-selects tag family by category |
| 1 | `resolve_category` | multilingual name → `OST_*` (static map, TS-only) |
| 2 | `get_element_info` | family/type/category/level/host/bbox |
| 2 | `place_and_configure` | atomic place + set params in one TransactionGroup |
| 2 | `execute_transaction_group` | multi-tool atomic batch with `dryRun` rollback |
| 3 | `duplicate_element` | copy with mm XYZ offset |
| 3 | `export_view_image` | export active/named view to PNG |
| 3 | `get_parameter_definitions` | full param schema (name, GUID, group, isShared) |

Also: **Revit 2027** build config (`Debug R27`/`Release R27`, net8.0) added to
`RevitMCPCommandSet.csproj`; all tools registered in `command.json`.

### Flexibility — arbitrary C#
`send_code_to_revit` (already in the repo) compiles and runs arbitrary C# via
Roslyn inside Revit — the escape hatch for anything the predefined tools don't
cover.

### Test panel
A **"Test Tools"** ribbon button (`plugin/Core/TestToolsCommand.cs` +
`plugin/UI/TestToolsWindow.xaml[.cs]`) opens a window that lists every tool with
per-tool Run + Run-All, a server status dot, element-ID picker, and formatted
JSON results — sends JSON-RPC to localhost:8080 on background threads.

---

## 8. How to run / verify

### Detector tests
```bash
cd C:\Users\DE1E7A\revit-personalization
python -m pytest tests/test_detector.py -v          # 12 tests
python -m detector.synthetic                          # dump a sample session JSONL
```

### Inspect logs / detected routines
```bash
python check_logs.py                                  # what the add-in wrote
python -c "from mcp_server.log_reader import list_candidate_routines as L; [print(r.id, r.support, r.confidence) for r in L()]"
```

### MCP server end-to-end check
```bash
python test_mcp_server.py                             # resources + tools (no Revit needed)
```

### Pattern-agent evaluation (needs ANTHROPIC_API_KEY)
```bash
python eval/run_experiment.py --k-values 1,3,5 --reps 3
```

### Chatbot UI
```bash
python chatbot/chat_server.py                         # http://localhost:5000
```

### mcp-servers-for-revit
```bash
cd C:\Users\DE1E7A\mcp-servers-for-revit\server
npm install && npm run build && npm start
# C# side: build commandset (Debug R25/R26/R27) in Visual Studio,
# then in Revit click "Revit MCP Switch" to start the TCP server (port 8080),
# and "Test Tools" to exercise every tool.
```

---

## 9. Models & MCP conventions

- Default Claude models: **Opus 4.8** (`claude-opus-4-8`) for pattern analysis,
  **Sonnet 4.6** (`claude-sonnet-4-6`) for macro generation. Use **adaptive
  thinking** (`thinking={"type":"adaptive"}`) — `budget_tokens` is removed on
  Opus 4.7+; do not use it.
- Anthropic SDK only (no OpenAI shims). Stream long requests.
- Revit internal units are decimal feet; convert mm → feet via `mm / 304.8`.

---

## 10. Key design decisions (for the write-up)

1. **v0.1 baseline = the literal char-shape matcher**, not a wrapper of the old
   `log_reader` episode-grouping (which was actually richer, "v1.5"). This keeps
   the precision/recall comparison meaningful — the baseline genuinely exhibits
   all four weaknesses.
2. **`key` derived in Python**, C# logger untouched (no re-deploy).
3. **Two ranking axes kept separate** (`support` frequency + `confidence`
   tightness) so routines can be ranked by either or both later.
4. **v0.2 is the default**; v0.1 reachable only by explicit selection, never a
   silent constant.
5. **Hosted families** (doors/windows) need a wall host —
   `place_element`/`place_and_configure` detect `OneLevelBasedHosted` and snap to
   the nearest wall centerline.

---

## 11. Status & next steps

**Done**
- v0.2 detector + v0.1 baseline behind one interface; 12 tests pass.
- `list_candidate_routines()` defaults to v0.2; v0.1 selectable.
- `CandidateRoutine.support` added; confidence semantics documented.
- mcp-servers-for-revit: 10 tools + Revit 2027 + Test Tools panel (branch
  `feature/tier1-3-tools`, committed locally).

**Open / next**
- [ ] Build `eval/detection_eval.py` — detection precision/recall/F1 per detector
      over a labeled synthetic session (the v0.1-vs-v0.2 comparison).
- [ ] Commit the v0.2 detector work to the revit-personalization repo.
- [ ] Fork mcp-servers-for-revit and push `feature/tier1-3-tools` (upstream
      remote currently rejects the push).
- [ ] Deploy updated hosted-family placement + verify in Revit.
- [ ] RevitWriteServer: implement Isolate/Zoom/Select commands (or fully migrate
      to mcp-servers-for-revit's `operate_element`).
