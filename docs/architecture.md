# System Architecture
## Agent-Augmented BIM Log Mining for Personalized Action Generation

---

## 1. System Overview

The system observes a Revit user's repetitive modelling actions, detects recurring workflows (called *Custom Element Instantiation routines*), and uses a multi-agent LLM pipeline to convert those routines into one-click shortcuts — all running locally, without uploading project data to the cloud.

### Autodesk Ecosystem Integration (Revit 2027)

Revit 2027 ships with two AI-related components that are relevant to this thesis:

| Autodesk Component | What it is | Our role |
|-------------------|-----------|---------|
| **Autodesk Public MCP Server** (Tech Preview) | A read-only MCP server exposed by Revit on `localhost:3000`. Supports model queries only: element counts, parameter values, view info. **Cannot create, modify, or delete elements.** | Used for model context queries (precondition checking) before suggesting shortcuts |
| **Autodesk Assistant** (Tech Preview) | An AI chat panel embedded inside Revit's UI. Supports natural language queries and task automation (schedules, tags). Can be extended by registering additional custom MCP servers. | Our Python MCP server is registered as an additional endpoint — users can ask "What shortcuts have I learned?" in natural language directly inside Revit |

**Key finding:** The Autodesk Public MCP Server is confirmed **read-only** in its current Tech Preview (April 2026). It cannot place elements, set parameters, or create annotation tags. All model modification goes through our C# add-in.

---

## 2. High-Level Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Revit 2027 (Windows process)                       │
│                                                                             │
│   ┌─────────────────────────────┐  ┌──────────────────────────────────────┐ │
│   │   Autodesk Assistant        │  │   C# RevitLogger Add-in              │ │
│   │   (chat panel, Tech Preview)│  │                                      │ │
│   │                             │  │  App.cs           (event wiring)     │ │
│   │  "What routines have I      │  │  ActionCapture.cs (DocumentChanged)  │ │
│   │   learned?"                 │  │  ElementSnapshot.cs (param diffs)    │ │
│   │       ↓ calls MCP tools     │  │  LogWriter.cs     (async JSONL)      │ │
│   │   ← our server.py answers   │  │  RoutineDetector.cs [TODO]           │ │
│   │                             │  │  ShortcutRunner.cs [TODO]            │ │
│   │   "Run door shortcut"       │  │  NotificationUI.xaml [TODO]          │ │
│   │       ↓ calls MCP tools     │  │         │              ↑             │ │
│   │   → execute_revit_command   │  │  writes JSONL    reads IPC files     │ │
│   └─────────────────────────────┘  └────────┬─────────────────────────────┘ │
│                                             │                               │
│   ┌─────────────────────────────┐           │ IPC: pending_execution.json   │
│   │ Autodesk Public MCP Server  │           │      execution_result_*.json  │
│   │ (read-only, localhost:3000) │           │                               │
│   │ - get_elements_by_category  │           │                               │
│   │ - get_active_view           │           │                               │
│   │ - get_loaded_families       │           │                               │
│   │ (no write operations)       │           │                               │
│   └─────────────────────────────┘           │                               │
└────────────────────────────────────────────┼────────────────────────────────┘
                                             │ writes JSONL log files
                                             ▼
                    %LOCALAPPDATA%\RevitPersonalization\
                    ├── logs\session_*.jsonl       ← action log
                    ├── shortcuts\*.json           ← saved shortcuts
                    └── ipc\                       ← Python↔C# IPC
                                             │
                                             │ Python reads logs + IPC
                                             ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                      Python MCP Server  (local process)                    │
│                      mcp_server/server.py  (FastMCP, port 3100)            │
│                                                                            │
│  Resources:                          Tools:                                │
│  logs://candidate_routines           analyze_pattern                       │
│  logs://routine/{id}/examples        generate_command                      │
│                                      execute_revit_command → IPC → C# add-in│
│                                      query_model → Autodesk Public MCP     │
│                                      list_shortcuts                        │
│                                                                            │
│  Registered as additional MCP endpoint in Autodesk Assistant settings      │
│  → Autodesk Assistant chat panel can call all tools above                  │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │ feeds examples to orchestrator
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                        Orchestrator (CLI / eval)                           │
│                        orchestrator/agents.py                              │
│                                                                            │
│  Pattern Agent  (claude-opus-4-7 + extended thinking)                     │
│    Input:  k example episodes                                              │
│    Output: Motif JSON (invariant steps + constant/variable param rules)    │
│                                                                            │
│  Macro Agent (claude-sonnet-4-6)                                           │
│    Input:  Motif JSON                                                      │
│    Output: MCP tool call sequence stored in ShortcutConfig.json            │
│                                                                            │
│  User confirms → ShortcutConfig saved to shortcuts/                        │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Flow

### 3.1 Logging Phase (always running while Revit is open)
```
User commits a Revit transaction
  → DocumentChanged event fires
  → ActionCapture.ProcessEvent()
      → Place / SetParam / Tag records created
      → ElementSnapshot provides before/after param diffs
  → Records enqueued in BlockingCollection
  → LogWriter background task writes JSONL line to disk
```

### 3.2 Detection Phase
```
[Option A — future: in-add-in, real-time]
  RoutineDetector.cs (TODO)
    → maintains rolling buffer of last 50 action records
    → checks for repeated structural signatures every N records
    → if ≥ 2 repetitions: signals NotificationUI.xaml

[Option B — current: Python, on demand]
  python orchestrator/agents.py --list
    → log_reader.py groups records by element_id into episodes
    → computes structural signatures per episode
    → groups with ≥ 2 identical signatures → CandidateRoutine
```

### 3.3 Extraction Phase (orchestrator or Autodesk Assistant)
```
Via CLI:
  python orchestrator/agents.py --routine-id <id> --k 5

Via Autodesk Assistant chat (after server registration):
  User: "What routines have I been repeating?"
  Assistant calls: logs://candidate_routines  → our MCP server
  Assistant responds: "You've placed Door-Passage-Single-Full_Lite
                       + Tag 4 times. Want to save this as a shortcut?"
  User: "Yes, save it"
  Assistant calls: generate_command(motif=..., name="Place Door + Tag")

In both cases:
  → Pattern Agent (claude-opus-4-7 + extended thinking)
       analyses k examples → extracts Motif
  → Macro Agent (claude-sonnet-4-6)
       converts Motif → tool call sequence
  → ShortcutConfig saved to shortcuts/{id}.json
```

### 3.4 Execution Phase (C# add-in via IPC)
```
User triggers execution (CLI --execute, WPF button, or Autodesk Assistant chat)
  → execute_revit_command(shortcut_id, params)  [MCP tool in server.py]
  → execute_shortcut(shortcut_id, params)       [revit_bridge.py]
      → writes ipc/pending_execution.json
      → polls for ipc/execution_result_{id}.json

C# add-in (ShortcutRunner.cs — TODO):
  → FileSystemWatcher detects pending_execution.json
  → reads shortcuts/{id}.json (ShortcutConfig)
  → opens Revit transaction
  → for each step in mcp_tool_sequence:
      "place_element"          → FamilyInstanceCreationData + doc.Create.NewFamilyInstance()
      "set_parameter"          → fi.get_Parameter(name).Set(value)
      "create_annotation_tag"  → IndependentTag.Create()
  → commits transaction
  → writes ipc/execution_result_{id}.json
  → Python reads result, returns to caller
```

### 3.5 Model Context Queries (Autodesk Public MCP Server, read-only)
```
[Before suggesting or executing a shortcut]
  query_model("get_loaded_families", {"category": "Doors"})
  query_model("get_active_view")
  query_model("get_elements_by_category", {"category": "Doors", "level": "L1"})
  → HTTP POST to localhost:3000 (Autodesk Public MCP Server)
  → returns JSON result (read-only, no model modification)
```

---

## 4. Autodesk Ecosystem Integration Details

### 4.1 Autodesk Public MCP Server — Read-Only Queries

The Public MCP Server exposes six tool groups. All are read-only:

| Tool group | Example tools | Our use |
|------------|--------------|---------|
| Model queries | `get_elements_by_category`, `get_element_parameters` | Precondition checks before shortcut execution |
| Sheet management | `get_sheets`, `get_views` | Not used |
| Room management | `get_rooms` | Not used |
| Schedules | `get_schedules` | Not used |
| Exports | `export_to_dwg` | Not used |
| Element operations | Element queries (read-only) | Family availability checks |

**Confirmed limitation (April 2026 Tech Preview):** *"The toolset is limited to read-only operations at the moment — no Revit modifications are possible."*

We call this server via `revit_bridge.model_query()`. If it is unreachable (Revit not open or MCP not enabled), execution continues without context — the shortcut runs without precondition checking.

### 4.2 Autodesk Assistant — Conversational Interface

The Autodesk Assistant is an AI chat panel embedded in Revit 2027. It natively supports adding custom local MCP servers as additional endpoints.

**How to register our Python MCP server with the Autodesk Assistant:**

1. Start the Python MCP server: `python mcp_server/server.py`
2. In Revit → Autodesk Assistant settings → Add MCP Server
3. Configure: `{"name": "revit-personalization", "url": "http://localhost:3100/sse"}`

Once registered, users can interact with our system in natural language inside Revit:

| User says in Assistant chat | What happens |
|----------------------------|-------------|
| "What repetitive routines have I been doing?" | Assistant calls `logs://candidate_routines` → our server returns detected routines |
| "Show me examples of my door placement routine" | Assistant calls `logs://routine/{id}/examples` |
| "Save my door routine as a shortcut" | Assistant calls `generate_command` → shortcut saved |
| "Run my door shortcut with Mark D-105" | Assistant calls `execute_revit_command` → C# add-in executes in Revit |
| "How many doors are on Level 1?" | Assistant calls Autodesk's own `get_elements_by_category` tool |

This makes the Autodesk Assistant the **natural language front-end** for our personalization system, directly addressing Research Gap 4 from the thesis (proactive, real-time shortcut suggestion).

### 4.3 Why Not Use Autodesk Assistant for Execution Directly?

The Assistant *can* perform some model modifications (creating schedules, tagging elements) via its own internal tool groups. However:
1. It operates via natural language prompts — not programmatic, reproducible tool call sequences
2. It has no concept of a "stored shortcut" or "learned motif"
3. It cannot be scripted to execute a specific sequence of steps with specific parameter values
4. It requires Autodesk's cloud services for the AI reasoning (privacy concern for some firms)

Our C# add-in executes shortcuts **deterministically** from a stored `ShortcutConfig.json` — the user confirms once, and every subsequent execution is identical.

---

## 5. Component Reference

### 5.1 C# Add-in — `RevitLogger/`

| File | Status | Responsibility |
|------|--------|---------------|
| `App.cs` | ✅ Done | `IExternalApplication` entry point; per-document session management |
| `ActionCapture.cs` | ✅ Done | `DocumentChanged` handler → Place / SetParam / Tag records |
| `ElementSnapshot.cs` | ✅ Done | Before/after parameter diff cache |
| `LogWriter.cs` | ✅ Done | Async JSONL writer (`BlockingCollection`, `System.Text.Json`) |
| `ActionRecord.cs` | ✅ Done | Enriched log schema (Jang & Lee 2023) |
| `SessionInfo.cs` | ✅ Done | Session metadata (SHA-1 hashed path, revit version) |
| `RoutineDetector.cs` | 🔲 TODO | Rolling buffer + repeated-subsequence detection → triggers UI |
| `ShortcutRunner.cs` | 🔲 TODO | FileSystemWatcher → executes shortcuts via Revit API transactions |
| `NotificationUI.xaml` | 🔲 TODO | WPF toast: "Learn as Shortcut?" / "Run" / "Dismiss" |

### 5.2 Python MCP Server — `mcp_server/`

| File | Responsibility |
|------|---------------|
| `log_reader.py` | JSONL parser; episode-grouping routine detector |
| `server.py` | FastMCP server; 5 tools + 2 resources; registered with Autodesk Assistant |
| `revit_bridge.py` | Two channels: `model_query()` (→ Autodesk read-only server) + `execute_shortcut()` (→ C# add-in IPC) |

### 5.3 Orchestrator — `orchestrator/`

| File | Model | Role |
|------|-------|------|
| `pattern_agent.py` | `claude-opus-4-7` + extended thinking | Extracts Motif from k examples |
| `macro_agent.py` | `claude-sonnet-4-6` | Converts Motif to tool call sequence |
| `agents.py` | — | CLI coordinator; also callable from MCP server tools |

### 5.4 Evaluation — `eval/run_experiment.py`

Measures Pattern Agent accuracy vs. k (number of examples). Produces `results/performance_vs_k.csv` for thesis §5 evaluation tables.

---

## 6. Python ↔ C# IPC Protocol

File-based inter-process communication via `%LOCALAPPDATA%\RevitPersonalization\ipc\`:

**Request** (Python writes, C# reads):
```json
// ipc/pending_execution.json
{
  "shortcut_id": "a1b2c3d4",
  "params": { "Mark": "D-105" },
  "requested_at": 1779509164.268
}
```

**Response** (C# writes, Python reads):
```json
// ipc/execution_result_a1b2c3d4.json
{
  "shortcut_id": "a1b2c3d4",
  "status": "success",
  "steps_executed": 3,
  "element_ids_created": [3327603, 3327683],
  "executed_at": 1779509167.5
}
```

The C# add-in uses `FileSystemWatcher` on the `ipc/` directory. Python polls for the result file with a 250ms interval, 30s timeout.

---

## 7. Technology Choices

| Choice | Rationale |
|--------|-----------|
| **C# / .NET 10** | Required: only Revit API can observe `DocumentChanged` and execute transactions |
| **File-based IPC** | No custom HTTP server needed in C#; `FileSystemWatcher` is built into .NET; survives process restarts |
| **Autodesk Public MCP (read-only)** | Used only for model context queries — the one thing it's good at |
| **C# add-in for execution** | Deterministic, local, no Autodesk cloud dependency, full Revit API access |
| **FastMCP registered with Autodesk Assistant** | Gives the system a natural language interface inside Revit without building a separate chat UI |
| **claude-opus-4-7 + extended thinking** | Highest reasoning for constant/variable parameter classification |
| **claude-sonnet-4-6** | Structured config generation; fast and cost-effective |
| **JSONL** | Append-only, line-by-line parseable, robust to Revit crashes |
| **No cloud data upload** | All logs, shortcuts, and API calls use only element IDs and parameter names |

---

## 8. Deployment Topology

```
Developer machine (Windows, Revit 2027 installed)
│
├── C:\Program Files\Autodesk\Revit 2027\
│   └── RevitAPI.dll  (reference only)
│
├── %APPDATA%\Autodesk\Revit\Addins\2027\
│   ├── RevitLogger.addin
│   └── RevitLogger.dll               (built + deployed via deploy.ps1)
│
├── %LOCALAPPDATA%\RevitPersonalization\
│   ├── logs\session_*.jsonl           (add-in writes)
│   ├── shortcuts\*.json              (orchestrator writes, add-in reads)
│   ├── ipc\pending_execution.json    (Python writes, C# reads)
│   └── ipc\execution_result_*.json  (C# writes, Python reads)
│
├── revit-personalization\            (this repository)
│   ├── mcp_server\server.py          python mcp_server/server.py  (port 3100)
│   ├── orchestrator\agents.py        python orchestrator/agents.py --routine-id ...
│   └── eval\run_experiment.py        python eval/run_experiment.py
│
└── Autodesk Public MCP Server        (bundled with Revit 2027, auto-starts, port 3000)
    Read-only model queries only
```

**Autodesk Assistant registration** (one-time setup):
```
Revit 2027 → Autodesk Assistant → Settings → Add MCP Server
  Name: revit-personalization
  URL:  http://localhost:3100/sse
```

---

## 9. Privacy and Data Handling

- **No geometry logged.** Only element IDs, categories, family names, parameter names/values.
- **File paths SHA-1 hashed.** The `document_hash` field is the first 12 hex chars of `SHA1(path)`.
- **All processing is local.** Claude API calls send only action type strings and parameter name/value pairs.
- **Autodesk Public MCP queries are local.** Port 3000 on the same machine — no data leaves the network.
- **Autodesk Assistant AI reasoning** is the one component that uses Autodesk's cloud. It only receives the text of the user's message and our MCP server's JSON responses — no model geometry or file paths.
