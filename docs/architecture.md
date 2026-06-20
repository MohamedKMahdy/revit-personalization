# System Architecture
## Agent-Augmented BIM Log Mining for Personalized Action Generation

---

## 1. System Overview

The system observes a Revit user's repetitive modelling actions, detects recurring
workflows (called *Custom Element Instantiation routines*), and uses a multi-agent LLM
pipeline to convert those routines into one-click shortcuts — all running **locally**,
without uploading project data to the cloud.

The implementation is split across **three repositories**, one per pipeline stage:

| Stage | Repo | Role |
|-------|------|------|
| **Observe** | `generalBIMlog` | C# `RevitLogger` add-in — logs the live action stream to a `ProjectSchema` JSON per project. **Never writes the model.** |
| **Detect + Generate** | `revit-personalization` *(this repo)* | Python pipeline — adapts the log to an action stream, mines routines, runs the two Claude agents, and presents detections in a chatbot. |
| **Execute** | `mcp-servers-for-revit` | C# plugin (a fork of the open-source `revit-mcp`) — runs predefined tool commands on Revit's UI thread; TCP JSON-RPC server on `localhost:8080`. |

Supported Revit versions: **2025 and 2026 (.NET 8)**. No project geometry leaves the
machine; the entire personalization runtime is local. (A separate, opt-in cloud
dataset-collection phase lives in the `generalBIMlog` repo and is out of scope here.)

### Why a native logging add-in *and* a separate execution backend
- There is no API event for "user just placed a door" outside Revit. Only the Revit API
  event system (`DocumentChanged`) can observe the live action stream → this requires a
  native C# add-in (the `generalBIMlog` logger).
- The Revit API is C#-only and must run inside Revit, so model writes go through a
  separate C# plugin (`mcp-servers-for-revit`) rather than being re-implemented in Python.

---

## 2. High-Level Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      Revit 2025 / 2026 (Windows process)                     │
│                                                                              │
│  ┌──────────────────────────┐ ┌──────────────────────────┐ ┌──────────────┐ │
│  │ generalBIMlog            │ │ mcp-servers-for-revit    │ │ BIMAssistant │ │
│  │  RevitLogger  (OBSERVE)  │ │  plugin       (EXECUTE)  │ │  (CHAT pane) │ │
│  │                          │ │                          │ │              │ │
│  │ DocumentChanged →        │ │ TCP JSON-RPC server      │ │ WebView2 →   │ │
│  │ CREATED/REVISED/DELETED  │ │ localhost:8080           │ │ chatbot:5000 │ │
│  │ element events           │ │ runs tool commands on    │ │ + "Open      │ │
│  │                          │ │ the Revit UI thread      │ │  Assistant"  │ │
│  │ writes ProjectSchema     │ │ (IExternalEventHandler)  │ │  ribbon btn  │ │
│  └───────────┬──────────────┘ └────────────▲─────────────┘ └──────┬───────┘ │
└──────────────┼─────────────────────────────┼─────────────────────┼──────────┘
               │ %APPDATA%\Autodesk\Revit\    │ TCP JSON-RPC         │ HTTP
               │  Addins\<ver>\RevitLogger\   │ (place/set/tag/      │ :5000
               │  Logs\eventlog\{guid}.json   │  operate)            │
               ▼                              │                      │
┌──────────────────────────────────────────────────────────────────┼──────────┐
│                     revit-personalization  (Python, local)        │          │
│                                                                   │          │
│  mcp_server/generalbimlog_reader.py   ProjectSchema → ActionRecord stream    │
│        │                                                          │          │
│  detector/  (v0.2 ClusterDetector default, v0.1 baseline)        │          │
│        │  → CandidateRoutine (support = frequency, confidence)    │          │
│        ▼                                                          │          │
│  orchestrator/                                                    │          │
│    pattern_agent.py  claude-opus-4-8 (adaptive thinking) → Motif  │          │
│    macro_agent.py    claude-sonnet-4-6 → MCP tool-call sequence   │          │
│        │                       │ grounds via revit_bridge read tools         │
│        ▼                       └──────────────────────────────────┘          │
│  chatbot/chat_server.py  (FastAPI :5000)  ── /api/execute ──► revit_bridge ──┘
│    • browsable PATTERN HISTORY (pattern_history.json)             (→ :8080)   │
│    • auto-starts pattern_watcher.py (scan logs → agents → notify)             │
│                                                                              │
│  mcp_server/server.py  (FastMCP, SSE :3100)  — optional MCP host interface   │
└──────────────────────────────────────────────────────────────────────────────┘
```

The chatbot UI is reachable either in a normal browser at `http://localhost:5000` or
embedded inside Revit via the `BIMAssistant` dockable WebView2 pane.

---

## 3. Data Flow

### 3.1 Logging Phase (generalBIMlog, always running while Revit is open)
```
User commits a Revit transaction
  → DocumentChanged event fires inside the generalBIMlog RevitLogger
  → a CREATED / REVISED / DELETED element event is recorded, each with the
    element's FULL parameter snapshot (state-free; not a delta)
  → appended to the project's ProjectSchema JSON:
    %APPDATA%\Autodesk\Revit\Addins\<ver>\RevitLogger\Logs\eventlog\{projectGUID}.json
```

### 3.2 Adaptation + Detection Phase (Python, on demand or via the watcher)
```
mcp_server/generalbimlog_reader.py
  → reads every ProjectSchema file, diffs consecutive snapshots
  → emits an ActionRecord stream: CREATED→Place/Tag, REVISED→SetParam
    (one per changed user-editable instance param), DELETED→Delete

detector/  (mcp_server/log_reader.list_candidate_routines)
  → v0.2 ClusterDetector (default): tokenize → segment by element_id →
    featurize → greedy average-linkage cluster → threshold → cooldown
  → clusters with ≥ N members become CandidateRoutines
    (support = cluster size = frequency; confidence = intra-cluster tightness)
```

### 3.3 Generation Phase (orchestrator, two Claude agents)
```
Via CLI:           python orchestrator/agents.py --routine-id <id> --k 5
Via the watcher:   pattern_watcher.py runs the same two agents on each new routine

  → Pattern Agent  (claude-opus-4-8, adaptive thinking)
       analyses k example episodes → extracts a generalised Motif JSON
       (invariant steps + constant/variable parameter rules)
  → Macro Agent    (claude-sonnet-4-6)
       optionally grounds against the live model (revit_bridge read tools),
       then maps the Motif → an ordered mcp-servers-for-revit tool-call sequence
  → user confirms → ShortcutConfig saved to
    %LOCALAPPDATA%\RevitPersonalization\shortcuts\{id}.json
```

### 3.4 Presentation Phase (chatbot + pattern history)
```
pattern_watcher.py → chatbot.trigger.notify_pattern() → POST /api/pattern
  → the detection is SAVED as its own record in pattern_history.json (status "new")
  → the chatbot (FastAPI :5000) streams a greeting from Claude (claude-opus-4-8)
  → the left sidebar lists every detection newest-first; a 5 s poll auto-surfaces
    fresh ones; clicking a record re-opens it with its conversation restored
  → the user asks questions / adjusts parameters, then confirms or dismisses
    (each pattern carries its own status: new / seen / executed / dismissed)
```

### 3.5 Execution Phase (mcp-servers-for-revit via the bridge)
```
User confirms in the chat (or CLI --execute)
  → POST /api/execute  →  revit_bridge.execute_shortcut(tool_sequence)
  → for each step, the bridge sends a JSON-RPC 2.0 command over TCP to
    the mcp-servers-for-revit C# plugin (localhost:8080):
      place_element          → create_point_based_element   (typeId resolved by name; mm)
      set_parameter          → set_element_parameter
      create_annotation_tag  → tag_element
  → the plugin runs each command on the Revit UI thread
    (IExternalEventHandler + RaiseAndWaitForCompletion) and returns an
    AIResult envelope {Success, Message, Response}
  → follow-up actions (Isolate / Select on the last placed element) use operate_element

Execution safety: only the allowlisted pipeline tools are dispatchable
(shared/tool_allowlist.py). The whole sequence is validated up front, so a tampered
shortcut or any non-allowlisted tool (e.g. send_code_to_revit) is rejected before any
step runs — there is NO passthrough of arbitrary tool names to the plugin.
```

### 3.6 Model Context Queries (read, for grounding)
```
[Before generating the tool sequence, the Macro Agent may ground itself]
  revit_bridge.model_query_state("door family types")
  → get_available_family_types / get_current_view_info / get_selected_elements …
  → JSON-RPC over TCP to the same mcp-servers-for-revit plugin (localhost:8080)
If the plugin is unreachable (Revit not open), generation continues without context
(--no-context) and execution is simply unavailable.
```

---

## 4. Component Reference

### 4.1 Logging add-in — `generalBIMlog` repo (separate)
The `RevitLogger` add-in subscribes to `DocumentChanged` and writes a `ProjectSchema`
JSON per project (one file per `{projectGUID}.json`) under
`%APPDATA%\Autodesk\Revit\Addins\<ver>\RevitLogger\Logs\eventlog\`. It is an
element-event model (CREATED/REVISED/DELETED) where every event carries the element's
full parameter snapshot. It is **observer-only** — it never modifies the model. See the
`generalBIMlog` repo for the authoritative schema.

### 4.2 Pipeline — `revit-personalization` (this repo)

| Path | Responsibility |
|------|---------------|
| `mcp_server/generalbimlog_reader.py` | Adapter: `ProjectSchema` → `ActionRecord` stream (snapshot diffing) |
| `mcp_server/log_reader.py` | Loads real action records; runs the selected detector (v0.2 default) |
| `detector/` | Routine-detection gate — `v2_cluster.py` (default), `v1_substring.py` (baseline), `v1_5_episode.py` (legacy) |
| `orchestrator/pattern_agent.py` | `claude-opus-4-8` + adaptive thinking → `Motif` |
| `orchestrator/macro_agent.py` | `claude-sonnet-4-6` → MCP tool-call sequence |
| `orchestrator/agents.py` | CLI coordinator (`--list`, `--routine-id`, `--k`, `--execute`, `--auto-confirm`, `--no-context`, `--params`) |
| `chatbot/chat_server.py` | FastAPI conversational UI (`:5000`) + browsable pattern history; auto-starts the watcher |
| `chatbot/trigger.py` | `notify_pattern()` → POST `/api/pattern` |
| `pattern_watcher.py` | Daemon: scan logs → run both agents on new routines → notify the chatbot |
| `mcp_server/revit_bridge.py` | TCP JSON-RPC client → `mcp-servers-for-revit` plugin (`:8080`) |
| `mcp_server/server.py` | FastMCP server (SSE `:3100`) — optional MCP host interface |
| `shared/schemas.py` | Pydantic v2 contract: `ActionRecord`, `CandidateRoutine`, `Motif`, `ShortcutConfig` |
| `shared/tool_allowlist.py` | Execution-safety allowlist |
| `BIMAssistant/` | C# in-Revit chat-pane add-in (WebView2 → chatbot `:5000` + ribbon button) |
| `eval/` | `run_experiment.py` (Pattern Agent quality vs. k), `detection_eval.py` (precision/recall/F1) |

> The in-repo `revit_addin/` folder is **retired** — logging is now sourced from
> `generalBIMlog` and execution from `mcp-servers-for-revit`.

### 4.3 Execution backend — `mcp-servers-for-revit` repo (separate)
A fork of the open-source `revit-mcp`. A C# plugin runs predefined tool commands on the
Revit UI thread and exposes a TCP JSON-RPC server on `localhost:8080`. An optional
TypeScript MCP server (`server/`) can translate MCP tool calls into the same TCP
protocol; this pipeline bypasses it and talks to the plugin's TCP socket directly. The
commandset builds for **R25/R26 only** and is built via GitHub Actions CI (corporate
NuGet `PackageSourceMapping` blocks `nuget.org` locally). A modeless **"Test Tools"**
window exercises every tool.

---

## 5. The action log model (`ActionRecord`)

`ActionRecord` (`shared/schemas.py`) is the pipeline's internal action model — it is
**synthesised** from the generalBIMlog `ProjectSchema`, not written directly by any
add-in. `action_type ∈ {Place, SetParam, Tag, Delete}`, keyed by `element_id`
(for a `Tag`, the labelled element is `tagged_element_id`). `key` is derived in the
Python featurizer (Place→`family_name`, SetParam→`param_name`, Tag→`tag_family_name`),
so detection keys can change without touching the C# logger.

---

## 6. Technology Choices

| Choice | Rationale |
|--------|-----------|
| **C# / .NET 8 for the in-Revit add-ins** | Only the Revit API can observe `DocumentChanged` (logging) and execute transactions (writes); Revit 2025/2026 target .NET 8 |
| **Separate observe / execute add-ins** | Single-responsibility: generalBIMlog only logs, mcp-servers-for-revit only writes — neither can corrupt the other's role |
| **TCP JSON-RPC to the execution plugin (`:8080`)** | Direct, language-agnostic socket; the Python bridge dispatches step by step on the Revit UI thread |
| **Snapshot-diff log adapter** | generalBIMlog is state-free (full snapshots); the adapter recovers per-parameter `SetParam` actions by diffing consecutive snapshots |
| **`claude-opus-4-8` + adaptive thinking (Pattern Agent)** | Highest reasoning for constant/variable parameter classification |
| **`claude-sonnet-4-6` (Macro Agent)** | Fast, strong at structured config generation |
| **Execution allowlist** | Deny-by-default — `send_code_to_revit` and any non-pipeline tool are unreachable through the bridge |
| **Local-only runtime** | All logs, shortcuts, and API calls stay on the machine; cloud dataset collection is a separate opt-in phase in generalBIMlog |

---

## 7. Deployment Topology

```
Developer machine (Windows, Revit 2025 / 2026 installed, .NET 8)
│
├── %APPDATA%\Autodesk\Revit\Addins\<ver>\
│   ├── RevitLogger\         (generalBIMlog — OBSERVE; writes ProjectSchema JSON)
│   ├── <mcp-servers-for-revit plugin .addin/.dll>   (EXECUTE; TCP :8080)
│   └── BIMAssistant.addin   (CHAT pane — WebView2 → chatbot :5000)
│
├── %LOCALAPPDATA%\RevitPersonalization\
│   ├── shortcuts\*.json              (orchestrator writes ShortcutConfigs)
│   ├── pattern_history.json          (chatbot — browsable detections + conversations)
│   ├── pattern_watcher_state.json    (watcher — already-announced routine ids)
│   └── .env                          (ANTHROPIC_API_KEY; gitignored)
│
└── revit-personalization\            (this repository — the Python pipeline)
    ├── chatbot\chat_server.py        python chatbot/chat_server.py   (:5000, auto-starts watcher)
    ├── pattern_watcher.py            scan logs → agents → notify the chatbot
    ├── orchestrator\agents.py        python orchestrator/agents.py --routine-id ...
    ├── mcp_server\server.py          python mcp_server/server.py     (FastMCP, SSE :3100)
    └── eval\                         run_experiment.py · detection_eval.py
```

---

## 8. Privacy and Data Handling

- **No geometry mined.** The pipeline reads element IDs, categories, family/type names,
  and parameter names/values — not geometry.
- **All processing is local.** Claude API calls send only action-type strings and
  parameter name/value pairs derived from the log; no model geometry or file paths.
- **Execution is local and explicit.** Shortcuts run only on user confirmation, only via
  the allowlisted bridge to the `mcp-servers-for-revit` plugin on `localhost:8080`.
- **History stays on disk.** `pattern_history.json` and shortcuts live under
  `%LOCALAPPDATA%` and never leave the machine.
- **Cloud collection is separate and opt-in.** An anonymised dataset-collection phase
  (Supabase EU) lives in the `generalBIMlog` repo and is independent of this local
  personalization runtime.
