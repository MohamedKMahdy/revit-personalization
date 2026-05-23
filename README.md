# Agent-Augmented BIM Log Mining — Thesis Implementation

## Architecture

```
Revit 2027 (C# add-in)  →  JSON logs  →  Python MCP Server  →  Multi-Agent Orchestrator
                                               ↕ bridges ↕
                                        Revit Public MCP Server
```

## Setup

### 1. Python environment
```powershell
pip install -r requirements.txt --user
```

### 2. API key
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

### 3. C# add-in (requires Revit 2027 + Visual Studio)
```powershell
cd RevitLogger
dotnet build
# Copy output + .addin file to %APPDATA%\Autodesk\Revit\Addins\2027\
```

## Running the pipeline

### Without Revit (synthetic logs)
```powershell
# Run the full agent pipeline on synthetic door routine
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5

# Auto-confirm (skip the y/N prompt)
python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5 --auto-confirm
```

### MCP server (for MCP Inspector or Autodesk Assistant)
```powershell
python mcp_server/server.py
```

### Evaluation (sample efficiency experiment)
```powershell
python eval/run_experiment.py --routine-id door_single_flush_tagged --k-values 1 3 5
```

## Project structure

```
revit-personalization/
├── RevitLogger/        C# Revit add-in — event logging, routine detection, WPF UI
├── mcp_server/         Python MCP server — exposes log resources + execution tools
├── orchestrator/       Multi-agent orchestrator — Pattern Agent + Macro Agent
├── shared/             Pydantic schemas shared between all components
├── tests/              Synthetic log data for testing without Revit
└── eval/               Sample efficiency evaluation harness
```

## Log format

Action records written by the C# add-in (and synthetic test data) use this schema:
```json
{ "action": "Place|SetParam|Tag", "timestamp": 1234567890.0,
  "elementId": 1001, "viewId": 301, "familyType": "M_Single-Flush:...",
  "paramName": "Fire Rating", "paramValue": "60", "tagFamily": "Door Tag" }
```
