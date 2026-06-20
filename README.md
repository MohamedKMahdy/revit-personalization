# Agent-Augmented BIM Log Mining
### Personalized Action Generation for Revit 2025/2026 — MSc Thesis Implementation

A system that watches a Revit user's repetitive modelling actions, detects recurring
workflows, and uses a multi-agent LLM pipeline (Claude API) to turn them into one-click
shortcuts — running **entirely locally**.

This repository is the **pipeline itself** — the thesis contribution. It is the middle
stage of a three-repo system:

1. **Observe** — the C# `RevitLogger` add-in in the separate
   [`generalBIMlog`](https://github.com/) repo logs the live action stream to a
   `ProjectSchema` JSON per project. It never writes the model.
2. **Detect + Generate** — *this repo* converts that log into an action stream, mines
   repeated routines, and runs two Claude agents (a Pattern Agent → generalised *motif*,
   a Macro Agent → executable tool-call sequence). A conversational chatbot surfaces each
   detection for confirmation.
3. **Execute** — confirmed shortcuts are dispatched to the `mcp-servers-for-revit`
   execution backend (a C# plugin listening on TCP `localhost:8080`), which runs the
   tool commands on Revit's UI thread.

No BIM logs leave the machine; the whole personalization runtime is local. (A separate,
opt-in cloud dataset-collection phase lives in the `generalBIMlog` repo and is out of
scope here.)

---

## Documentation

| Document | Contents |
|----------|---------|
| [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) | Single source of truth — architecture, file map, run instructions, design decisions |
| [`docs/architecture.md`](docs/architecture.md) | Full system architecture — components, data flow, technology choices, deployment topology |
| [`docs/abstract.md`](docs/abstract.md) | Thesis abstract |
| [`docs/compliance-analysis.md`](docs/compliance-analysis.md) | Privacy / data-handling analysis |
| [`detector/README.md`](detector/README.md) | Routine-detection algorithms (v0.2 default, v0.1 baseline) |
| [`eval/README.md`](eval/README.md) | Evaluation harnesses (Pattern Agent quality, detection precision/recall) |

---

## Quick Start

### 1. Python environment
```powershell
pip install -r requirements.txt --user
```

### 2. API key
Create a `.env` in the repo root (see [`.env.example`](.env.example)):
```ini
ANTHROPIC_API_KEY=sk-ant-...
```
or set it in the environment:
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

### 3. The C# add-ins (separate concerns, separate repos)
This repo no longer ships a logging add-in. The two C# pieces live elsewhere:

- **Logging (observer):** the `RevitLogger` add-in in the **`generalBIMlog`** repo.
  Install it into Revit 2025/2026; it writes a `ProjectSchema` JSON per project.
- **Execution backend:** the **`mcp-servers-for-revit`** plugin (a fork of the
  open-source `revit-mcp`). Build + install it into Revit; in Revit click
  **"Revit MCP Switch"** to start its TCP server on `localhost:8080`.

> The in-repo `revit_addin/` folder is **retired** — logging is now sourced from
> `generalBIMlog` and execution from `mcp-servers-for-revit`. It is kept only for
> historical reference.

Optionally, the in-Revit **BIM Assistant** chat pane (`BIMAssistant/`) is a third,
standalone add-in that embeds the chatbot UI inside Revit (see below).

---

## Running the Pipeline

### Orchestrator CLI (`orchestrator/agents.py`)
```powershell
# List all detected candidate routines (real generalBIMlog logs + synthetic test data)
python orchestrator/agents.py --list

# Run the full pipeline on a synthetic door routine (no Revit needed)
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5

# Run on a routine captured from your real Revit session
python orchestrator/agents.py --routine-id <id from --list> --k 3

# Skip the save-shortcut confirmation prompt (useful for eval / scripting)
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5 --auto-confirm

# Skip the live model-context query when Revit is not running
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5 --no-context

# Execute the shortcut live in Revit (requires Revit + the mcp-servers-for-revit plugin on :8080)
python orchestrator/agents.py --routine-id <id> --execute

# Provide runtime parameter overrides as JSON
python orchestrator/agents.py --routine-id <id> --execute --params '{"Mark":"D-101"}'
```

### Chatbot + BIM Assistant (`chatbot/chat_server.py`)
The conversational front-end. It presents each detected routine, lets the user adjust
parameters, then executes or dismisses. Every detection is persisted as its own record
(with its own conversation) in a browsable **pattern history** — a left sidebar lists
detections newest-first; clicking one re-opens it with the conversation restored, and a
5 s poll auto-surfaces fresh detections.

```powershell
# Start the assistant at http://localhost:5000 (auto-opens a browser,
# and auto-starts pattern_watcher.py in the background)
python chatbot/chat_server.py

# Don't open a browser / don't auto-start the watcher
python chatbot/chat_server.py --no-browser --no-watcher

# Seed the built-in sample pattern for UI testing only
python chatbot/chat_server.py --demo

# Ignore any saved history this run
python chatbot/chat_server.py --fresh
```

`pattern_watcher.py` (auto-started by the chatbot) restores the detect→notify bridge:
it scans the `generalBIMlog` logs and pushes new routines (support ≥ N) to the assistant.
```powershell
python pattern_watcher.py                 # watch forever (default 15 s / support 3)
python pattern_watcher.py --once          # one scan, then exit
python pattern_watcher.py --once --dry-run # detect + generate, but don't notify
```

**In-Revit BIM Assistant pane** (`BIMAssistant/`): a standalone Revit add-in that adds an
"Open Assistant" ribbon button and a dockable WebView2 pane pointed at the chatbot
(`http://127.0.0.1:5000`). It hosts the chat UI inside Revit — no logging, no model writes.

### MCP server (optional)
```powershell
# FastMCP server (SSE transport, port 3100) — for MCP Inspector or an MCP-aware host
python mcp_server/server.py
```

### Evaluation
```powershell
# Pattern Agent quality vs. number of examples (k)
python eval/run_experiment.py --k-values 1,2,3,5 --reps 3

# Detection precision/recall/F1 (v0.2 vs v0.1 baselines)
python eval/detection_eval.py
```

### Tests
```powershell
pytest                  # collects tests/ (49 passing); pytest.ini sets testpaths=tests
```

---

## Project Structure

```
revit-personalization/
├── PROJECT_OVERVIEW.md          Single source of truth (architecture, file map, decisions)
├── README.md, METHODOLOGY.md    Thesis write-up
├── requirements.txt             mcp, anthropic, httpx, pydantic, fastapi, uvicorn, pytest
├── pattern_watcher.py           Daemon: scans generalBIMlog logs → notifies the chatbot
├── deploy.ps1, setup_revit_env.py   Helpers (env bootstrap)
│
├── docs/
│   ├── architecture.md          Full system architecture
│   ├── abstract.md              Thesis abstract
│   ├── compliance-analysis.md   Privacy / data-handling analysis
│   └── revit-plugin.md          Logger field reference & semantic-logging rationale (historical)
│
├── shared/
│   ├── schemas.py               Pydantic v2 models — the contract between all components
│   │                            (ActionRecord, CandidateRoutine, Motif, ShortcutConfig)
│   └── tool_allowlist.py        Execution-safety allowlist (no send_code_to_revit)
│
├── detector/                    ★ Routine-detection gate (deterministic, no Revit calls)
│   ├── v2_cluster.py            ClusterDetector (DEFAULT, v0.2)
│   ├── v1_substring.py          SubstringDetector (baseline, v0.1)
│   ├── v1_5_episode.py          EpisodeGroupingDetector (legacy comparison, v1.5)
│   ├── base.py, _common.py      Detector protocol + shared featurizers
│   ├── synthetic.py             Synthetic-log generator
│   └── __init__.py              make_detector() factory (default "v2")
│
├── mcp_server/
│   ├── generalbimlog_reader.py  Adapter: generalBIMlog ProjectSchema → ActionRecord stream
│   ├── log_reader.py            Loads real action records, runs the selected detector
│   ├── server.py                FastMCP server (resources + tools, SSE :3100)
│   └── revit_bridge.py          TCP JSON-RPC client → mcp-servers-for-revit plugin (:8080)
│
├── orchestrator/
│   ├── agents.py                CLI orchestrator — coordinates both agents
│   ├── pattern_agent.py         claude-opus-4-8 + adaptive thinking → Motif
│   └── macro_agent.py           claude-sonnet-4-6 → MCP tool-call sequence
│
├── chatbot/
│   ├── chat_server.py           FastAPI conversational UI + pattern history (:5000)
│   └── trigger.py               notify_pattern() — pushes a detection to the server
│
├── BIMAssistant/                C# in-Revit chat pane (WebView2 → chatbot :5000) + ribbon button
│
├── eval/
│   ├── run_experiment.py        Pattern Agent quality vs. k
│   └── detection_eval.py        Detector precision/recall/F1 + ARI
│
├── tests/                       pytest suite (49 passing)
│   └── synthetic_logs/*.json    Pre-grouped CandidateRoutine fixtures
│
├── revit_addin/                 ⚠ RETIRED — superseded by generalBIMlog (log) +
│                                  mcp-servers-for-revit (execute). Kept for reference only.
├── check_logs.py                Diagnostic: dump detected routines
└── .env.example                 Required and optional environment variables
```

---

## Log Format (the input)

The pipeline's log **source** is now the `generalBIMlog` `RevitLogger` add-in, which
writes one `ProjectSchema` JSON per project:

```
%APPDATA%\Autodesk\Revit\Addins\<ver>\RevitLogger\Logs\eventlog\{projectGUID}.json
```

It is an element-event model — each session is a list of `CREATED` / `REVISED` /
`DELETED` events, where every event carries the element's **full** parameter snapshot
(not a delta). The adapter
[`mcp_server/generalbimlog_reader.py`](mcp_server/generalbimlog_reader.py) converts that
into the pipeline's `ActionRecord` stream (`Place` / `SetParam` / `Tag` / `Delete`,
keyed by `element_id`): `CREATED` → `Place` (or `Tag` for annotations); `REVISED` →
one `SetParam` per *changed user-editable* instance parameter (recovered by diffing
consecutive snapshots); `DELETED` → `Delete`.

See the `generalBIMlog` repo for the authoritative `ProjectSchema` definition, and
[`shared/schemas.py`](shared/schemas.py) for the `ActionRecord` contract the rest of the
pipeline consumes.
