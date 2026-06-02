# System Architecture
## Agent-Augmented BIM Log Mining for Personalized Action Generation

---

## 1. System Overview

The system observes a Revit user's repetitive modelling actions, automatically detects recurring workflows (called *Custom Element Instantiation routines*), and uses a multi-agent LLM pipeline to convert those routines into one-click shortcuts — all running locally, without uploading any project data to the cloud.

The three core concerns map directly to the three subsystems:

| Concern | Subsystem | Technology |
|---------|-----------|-----------|
| **Observe** — capture what the user does in real time | C# Revit Add-in | .NET 10 / Revit 2027 API |
| **Understand** — find patterns and extract intent | Python MCP Server + Orchestrator | FastMCP, Claude API |
| **Act** — replay learned routines on demand | Revit Public MCP Server bridge | HTTP / JSON-RPC |

---

## 2. High-Level Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Revit 2027 (Windows)                         │
│                                                                      │
│   User places doors, sets parameters, adds tags                      │
│            │                                                         │
│            │  DocumentChanged event (Revit API)                      │
│            ▼                                                         │
│  ┌─────────────────────┐                                             │
│  │   C# Add-in         │  IExternalApplication                       │
│  │   RevitLogger       │  ─ ActionCapture.cs  (event handler)        │
│  │                     │  ─ ElementSnapshot.cs (before/after diffs)  │
│  │                     │  ─ LogWriter.cs       (async JSONL writer)  │
│  └──────────┬──────────┘                                             │
│             │ writes JSONL                                           │
└─────────────┼────────────────────────────────────────────────────────┘
              │
              ▼
   %LOCALAPPDATA%\RevitPersonalization\logs\
   session_YYYYMMDD_HHmmss_<docHash>.jsonl
              │
              │  Python reads on demand
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Python MCP Server  (local process)                │
│                                                                      │
│  log_reader.py     ─ parses JSONL, groups by element, detects       │
│                      repeated episode signatures                     │
│  server.py         ─ FastMCP: exposes resources + tools             │
│  revit_bridge.py   ─ HTTP client → Revit Public MCP Server          │
│                                                                      │
│  Resources:  logs://candidate_routines                              │
│              logs://routine/{id}/examples                           │
│                                                                      │
│  Tools:      analyze_pattern   generate_command                     │
│              execute_revit_command   list_shortcuts                 │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ feeds CandidateRoutine + examples
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Orchestrator (CLI / eval)                      │
│                                                                      │
│  agents.py                                                          │
│    │                                                                 │
│    ├──▶  pattern_agent.py   claude-opus-4-7 + extended thinking     │
│    │       Input:  k example episodes                               │
│    │       Output: Motif JSON (invariant steps + param rules)       │
│    │                                                                 │
│    └──▶  macro_agent.py     claude-sonnet-4-6                       │
│            Input:  Motif JSON                                        │
│            Output: MCP tool call sequence (dry-run shown to user)   │
│                                                                      │
│  User confirms → ShortcutConfig saved to disk                       │
└──────────────────────────┬───────────────────────────────────────────┘
                           │  execute_revit_command (optional)
                           ▼
              Revit Public MCP Server  (localhost:3000)
              place_element / set_parameter / create_annotation_tag
                           │
                           ▼
                    Live Revit model updated
```

---

## 3. Data Flow

### 3.1 Logging Phase (always running while Revit is open)

```
User action in Revit
  → Revit commits transaction
  → DocumentChanged event fires
  → ActionCapture.ProcessEvent()
      → HandleAdded()   for Place + Tag
      → HandleModified() for SetParam (using ElementSnapshot diff)
  → ActionRecord enqueued in BlockingCollection
  → LogWriter background task dequeues and writes JSONL line
```

Each session file looks like:
```
Line 1:  {"record_type":"session_start", "session_id":"...", ...}
Line 2+: {"action_type":"Place", "element_id":..., "family_name":..., ...}
Line N:  {"record_type":"session_end", "session_id":"...", ...}
```

### 3.2 Detection Phase (on demand)

```
log_reader.list_candidate_routines()
  → reads all session_*.jsonl files
  → for each file, groups ActionRecords by element_id
  → forms episodes: records where element_id was first seen as a Place
  → computes structural signature per episode:
       "<category>|<family>|Place,SetParam(Mark),Tag"
  → groups episodes by signature
  → signatures with ≥ 2 occurrences → CandidateRoutine
```

### 3.3 Extraction Phase (orchestrator)

```
agents.py --routine-id <id> --k 5
  → fetch 5 examples from log_reader
  → Pattern Agent (claude-opus-4-7, extended thinking 8000 tokens)
       analyses all 5 examples
       identifies invariant steps
       classifies each SetParam as constant vs. variable
       → returns Motif JSON
  → Macro Agent (claude-sonnet-4-6)
       translates Motif → Revit MCP tool call sequence
       → returns list[{"tool":..., "arguments":{...}}]
  → user sees dry-run, confirms
  → ShortcutConfig saved as JSON in shortcuts/
```

### 3.4 Execution Phase (optional)

```
agents.py --execute
  → loads ShortcutConfig from disk
  → resolves {{location}} from user click (future: C# WPF shortcut button)
  → revit_bridge.execute_mcp_tool_sequence()
       POST http://localhost:3000/  (Revit Public MCP Server)
       {"jsonrpc":"2.0", "method":"tools/call",
        "params":{"name":"place_element", "arguments":{...}}}
  → Revit places the element, sets parameters, adds tag
```

---

## 4. Component Reference

### 4.1 C# Add-in — `RevitLogger/`

| File | Responsibility |
|------|---------------|
| `App.cs` | `IExternalApplication` entry point; manages per-document sessions; subscribes to `DocumentChanged`, `DocumentOpened`, `DocumentClosing` |
| `ActionCapture.cs` | Translates `DocumentChangedEventArgs` into `ActionRecord` objects; filters to authoring categories |
| `ElementSnapshot.cs` | In-memory parameter cache; computes before/after diffs for SetParam records |
| `LogWriter.cs` | Thread-safe async JSONL writer using `BlockingCollection<object>` and `System.Text.Json` |
| `ActionRecord.cs` | C# DTO: the enriched BIM log schema (snake_case, `[JsonPropertyName]` attributes) |
| `SessionInfo.cs` | Session metadata record written as line 1 of every JSONL file |
| `RevitLogger.addin` | Revit add-in manifest; deployed to `%APPDATA%\Autodesk\Revit\Addins\2027\` |

**Key design decisions:**
- The add-in is **logging-only** — no pattern detection, no AI, no UI. All intelligence lives in Python.
- `DocumentChanged` fires once per committed transaction, not per UI gesture — so one undo-step = one event, giving clean atomic grouping via `transaction_id`.
- A `BlockingCollection` decouples Revit's UI thread from disk I/O. The write loop runs on a thread pool thread and is never blocked by the UI.

### 4.2 Shared Schemas — `shared/schemas.py`

Pydantic v2 models that are the **contract** between all Python components. Every field name is snake_case to match the JSON keys written by the C# add-in.

| Model | Used by |
|-------|---------|
| `ActionRecord` | log_reader (parsing), orchestrator (input to agents) |
| `RoutineExample` | log_reader (grouping), orchestrator (agent input) |
| `CandidateRoutine` | MCP server resources, orchestrator input |
| `MotifStep` / `Motif` | Pattern Agent output, Macro Agent input, server tools |
| `ShortcutConfig` | Saved to disk; loaded for execution |

### 4.3 Python MCP Server — `mcp_server/`

| File | Responsibility |
|------|---------------|
| `log_reader.py` | Parses JSONL files; episode-grouping routine detection algorithm |
| `server.py` | FastMCP server; 2 resources, 4 tools; `_motif_to_tool_sequence()` helper |
| `revit_bridge.py` | HTTP client for the Revit Public MCP Server (localhost:3000) |

The MCP server plays two roles:
1. **Offline** (no Revit running): serves as a data API for the orchestrator to fetch candidate routines and examples.
2. **Online** (Revit running with Public MCP Server): acts as a bridge to execute learned shortcuts back into the live model.

### 4.4 Orchestrator — `orchestrator/`

| File | Model | Role |
|------|-------|------|
| `pattern_agent.py` | `claude-opus-4-7` + extended thinking (8 000 token budget) | Generalises k examples into a Motif — distinguishes constant from variable parameters |
| `macro_agent.py` | `claude-sonnet-4-6` | Converts Motif into an ordered list of Revit MCP tool calls |
| `agents.py` | — | CLI orchestrator; coordinates both agents; handles confirmation and saving |

**Why two separate agents?**
- Pattern extraction requires deep reasoning across multiple examples to correctly classify parameter variability — hence Opus + extended thinking.
- Tool sequence generation is a structured translation task with a fixed schema — Sonnet is faster and cheaper and handles it well.
- Separating the two makes each agent independently testable and lets us swap models without affecting the other.

### 4.5 Evaluation Harness — `eval/run_experiment.py`

Measures how the Pattern Agent's accuracy scales with k (number of examples shown). For each (routine, k, repetition) cell it:
- Calls the Pattern Agent
- Scores the returned Motif against the ground-truth episode structure
- Records `step_match_accuracy`, `param_coverage`, token usage, and latency
- Writes `results/performance_vs_k.csv` for the thesis §5 evaluation tables

---

## 5. Technology Choices

| Choice | Rationale |
|--------|-----------|
| **C# / .NET 10** | Required by the Revit 2027 API; no other language can subscribe to `DocumentChanged` |
| **System.Text.Json** (no NuGet) | Organisation policy blocks external NuGet sources; built-in since .NET 8 |
| **JSONL (JSON Lines)** | Append-only, streamable, one record per line — robust to incomplete writes if Revit crashes |
| **FastMCP (Python)** | Declarative MCP server definition; single file; compatible with MCP Inspector and Claude Desktop |
| **Pydantic v2** | Strong runtime validation of the schema contract; `model_dump()` / `model_validate_json()` for serialisation |
| **claude-opus-4-7 + extended thinking** | Highest reasoning depth for the parameter-classification task (constant vs. variable) — the core thesis contribution |
| **claude-sonnet-4-6** | Structured config generation; fast; no deep reasoning required for tool-call translation |
| **No cloud data upload** | All logs, shortcuts, and model calls use only the element IDs and parameter names — no geometry, no project contents |

---

## 6. Deployment Topology

```
Developer machine (Windows, Revit 2027 installed)
│
├── C:\Program Files\Autodesk\Revit 2027\
│   └── RevitAPI.dll, RevitAPIUI.dll  (Revit SDK — reference only, not shipped)
│
├── %APPDATA%\Autodesk\Revit\Addins\2027\
│   ├── RevitLogger.addin             (add-in manifest)
│   └── RevitLogger.dll               (built from RevitLogger/RevitLogger.csproj)
│
├── %LOCALAPPDATA%\RevitPersonalization\
│   ├── logs\session_*.jsonl          (written by add-in at runtime)
│   └── shortcuts\*.json             (written by orchestrator after confirmation)
│
└── revit-personalization\            (this repository)
    ├── mcp_server\server.py          (run manually: python mcp_server/server.py)
    ├── orchestrator\agents.py        (run manually: python orchestrator/agents.py ...)
    └── eval\run_experiment.py        (run for thesis evaluation)
```

The Revit Public MCP Server (Autodesk, `localhost:3000`) is a separate optional component. The entire logging and agent pipeline functions without it; it is only needed for the final *execution* step (applying a shortcut back into Revit).

---

## 7. Privacy and Data Handling

- **No geometry is logged.** Only element IDs, category names, family names, parameter names, and parameter values are captured.
- **File paths are SHA-1 hashed.** The `document_hash` field in `session_start` contains the first 12 hex characters of `SHA1(doc.PathName.ToLower())`. The full path never leaves the machine.
- **All processing is local.** The Claude API calls send only action type strings and parameter name/value pairs — no model geometry, no project file contents.
- **Logs are session-scoped.** Each Revit session writes to its own `.jsonl` file. Closing Revit flushes and seals the file with a `session_end` record.
