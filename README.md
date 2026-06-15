# Agent-Augmented BIM Log Mining
### Personalized Action Generation for Revit 2027 — MSc Thesis Implementation

A system that watches a Revit user's repetitive modelling actions, detects recurring workflows, and uses a multi-agent LLM pipeline (Claude API) to turn them into one-click shortcuts — running entirely locally.

---

## Documentation

| Document | Contents |
|----------|---------|
| [`docs/architecture.md`](docs/architecture.md) | Full system architecture — components, data flow, technology choices, deployment topology |
| [`docs/revit-plugin.md`](docs/revit-plugin.md) | Revit add-in deep-dive — every logged field explained, event handling, ElementSnapshot mechanism, Revit 2027 API changes |

---

## Quick Start

### 1. Python environment
```powershell
pip install -r requirements.txt --user
```

### 2. API key
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

### 3. C# add-in (Revit 2025/2026 → .NET 8, Revit 2027 → .NET 10)
```powershell
python setup_revit_env.py                                  # writes REPO_ROOT/PYTHON_EXE for the add-in
dotnet build revit_addin\RevitLogger.csproj -c Release     # default Revit 2027 (net10)
# for 2026:  dotnet build revit_addin\RevitLogger.csproj -c Release -p:RevitVersion=2026
.\deploy.ps1                                               # close Revit first (add -RevitVersion 2026 if needed)
```

---

## Running the Pipeline

```powershell
# See all detected routines (real logs + synthetic test data)
python orchestrator/agents.py --list

# Run full pipeline on synthetic door routine (no Revit needed)
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5

# Run on a routine captured from your real Revit session
python orchestrator/agents.py --routine-id <id from --list> --k 3

# Run with auto-confirm (skip the y/N prompt — useful for eval)
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5 --auto-confirm

# Execute the shortcut live in Revit (requires Revit Public MCP Server on :3000)
python orchestrator/agents.py --routine-id <id> --execute

# Start the MCP server (for MCP Inspector or Claude Desktop)
python mcp_server/server.py

# Run the evaluation harness (Pattern Agent accuracy vs. k)
python eval/run_experiment.py --k-values 1,2,3,5 --reps 3
```

---

## Project Structure

```
revit-personalization/
├── docs/
│   ├── architecture.md       Full system architecture
│   └── revit-plugin.md       Revit add-in: data collected and why
│
├── revit_addin/             C# Revit add-in — observer/logger (Revit 2025/26 .NET 8, 2027 .NET 10)
│   ├── App.cs                IExternalApplication entry point
│   ├── ActionCapture.cs      DocumentChanged event handler
│   ├── ElementSnapshot.cs    Before/after parameter diff cache
│   ├── LogWriter.cs          Async JSONL writer (BlockingCollection)
│   ├── ActionRecord.cs       Log schema DTO
│   ├── RoutineDetector.cs    Real-time CEI routine detection
│   ├── PatternBridge.cs      Notifies BIM Assistant panel on a repeat
│   ├── SessionInfo.cs        Session metadata DTO
│   └── RevitLogger.addin     Add-in manifest
│
├── shared/
│   └── schemas.py            Pydantic v2 models (contract between all Python components)
│
├── mcp_server/
│   ├── log_reader.py         JSONL parser + episode-grouping routine detector
│   ├── server.py             FastMCP server (resources + tools)
│   └── revit_bridge.py       HTTP client → Revit Public MCP Server
│
├── orchestrator/
│   ├── agents.py             CLI orchestrator — coordinates both agents
│   ├── pattern_agent.py      claude-opus-4-7 + extended thinking → Motif
│   └── macro_agent.py        claude-sonnet-4-6 → MCP tool call sequence
│
├── eval/
│   └── run_experiment.py     Pattern Agent accuracy vs. k evaluation harness
│
├── tests/
│   └── synthetic_logs/       Synthetic JSONL for testing without Revit
│       ├── door_routine_x5.json
│       └── window_routine_x3.json
│
├── results/                  Created by eval harness (gitignored)
├── .env.example              Required and optional environment variables
├── check_logs.py             Quick diagnostic: shows what the add-in has logged
└── deploy.ps1                Copies built DLL to Revit add-ins folder
```

---

## Log Format

The C# add-in writes JSONL files to `%LOCALAPPDATA%\RevitPersonalization\logs\`.  
Each line is one JSON object. Three record types:

```jsonc
// Line 1 — session metadata
{"record_type":"session_start","session_id":"sess_20260523035731",
 "revit_version":"Autodesk Revit 2027","document_hash":"69003c58b5f0", ...}

// Action records (Place / SetParam / Tag)
{"action_type":"Place","element_id":3327603,"element_category":"Doors",
 "family_name":"Door-Passage-Single-Full_Lite","type_name":"36\" x 84\"",
 "level_name":"L1 - Block 35","transaction_name":"Door", ...}

{"action_type":"SetParam","element_id":3327603,"param_name":"Mark",
 "param_value_before":"3C09","param_value_after":"DD1", ...}

{"action_type":"Tag","element_id":3327683,
 "tag_family_name":"Door Tag","tagged_element_id":3327603, ...}

// Last line — session closed cleanly
{"record_type":"session_end","session_id":"sess_20260523035731", ...}
```

See [`docs/revit-plugin.md`](docs/revit-plugin.md) for a full field-by-field explanation.
