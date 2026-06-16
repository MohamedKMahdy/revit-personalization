# revit_addin — Personalization Revit Add-in (observer) — RETIRED

> **RETIRED (2026-06-16):** the pipeline now sources its logs from the
> **generalBIMlog `RevitLogger`** instead of this plugin. `mcp_server/log_reader.py`
> reads generalBIMlog output via `mcp_server/generalbimlog_reader.py`; this add-in
> is no longer built, deployed, or read. Kept for reference/history only.

The Revit-side client of the personalization system. It is **observer-only**: it
watches the user's authoring actions and writes structured logs — it **never writes
to the model**. All model writes go through `mcp-servers-for-revit` (see
`mcp_server/revit_bridge.py`).

> This is **not** the general BIM logger. The supervisor's cross-platform logger
> lives in the separate [`generalBIMlog`](https://github.com/MohamedKMahdy/generalBIMlog)
> repo and uses a different schema (full element state per CREATED/REVISED/DELETED
> event). This add-in logs **action deltas** (`Place / SetParam / Tag / Delete`)
> tuned for Custom Element Instantiation (CEI) routine mining, and its JSON keys
> match `shared/schemas.py` / `mcp_server/log_reader.py`.

## What it does

```
User models in Revit
  → DocumentChanged event
    → ActionCapture        translates the event into an enriched ActionRecord
      → LogWriter          appends JSONL to %LOCALAPPDATA%\RevitPersonalization\logs\
      → RoutineDetector    real-time CEI episode tracking (Place → SetParam* → Tag?)
        → PatternBridge    on a repeat, hands the pattern to the Python BIM Assistant
                           chatbot (writes pending_pattern.json + launches
                           chatbot/notify_from_file.py → opens http://localhost:5000)
```

## Files

| File | Role |
|---|---|
| `App.cs` | `IExternalApplication` entry point; per-document session management |
| `ActionCapture.cs` | `DocumentChanged` → `ActionRecord` (sketch/stair deferral, host/geometry, Revit 2027 API) |
| `ActionRecord.cs` | The log schema (`schema_version: "2.0"`, snake_case JSON, matches Python) |
| `LogWriter.cs` | Async JSONL writer (`session_*.jsonl`) |
| `ElementSnapshot.cs` | Tracks parameter values to emit before/after diffs on `SetParam` |
| `SessionInfo.cs` | `session_start` record (project hash, Revit version) |
| `RoutineDetector.cs` | Real-time CEI episode detection (mirrors `log_reader.py` for ID compatibility) |
| `PatternBridge.cs` | On a repeat, launches the Python chatbot notifier (`http://localhost:5000`) |
| `ShortcutRunner.cs` | Retired stub — execution moved to `mcp-servers-for-revit` |

## Build & deploy

Supported: **Revit 2025 and 2026** (both host **.NET 8** → `net8.0-windows`). The
`RevitVersion` property (default 2026) selects which API DLLs are referenced. Revit
2027 is dropped for now — it hosts **.NET 10** and would need `net10.0-windows`.

```powershell
# 1. tell the add-in where Python + this repo live (run with the SAME Python you
#    use for the chatbot — writes REPO_ROOT/PYTHON_EXE to %LOCALAPPDATA%\...\.env)
python setup_revit_env.py

# 2. build (default Revit 2026; use -p:RevitVersion=2025 for Revit 2025)
dotnet build revit_addin\RevitLogger.csproj -c Release

# 3. copy the DLL into the Revit add-ins folder (close Revit first — DLL is locked while open)
.\deploy.ps1
```

The `.addin` manifest (`RevitLogger.addin`) deploys once to
`%APPDATA%\Autodesk\Revit\Addins\<version>\` and points at `RevitLogger.dll`.

Logs land in `%LOCALAPPDATA%\RevitPersonalization\logs\` (`session_*.jsonl`, plus
`_diag.txt` for troubleshooting).

## How the assistant is triggered

When `RoutineDetector` confirms a repeat, `PatternBridge`:

1. writes the pattern to `%LOCALAPPDATA%\RevitPersonalization\pending_pattern.json`, then
2. launches `PYTHON_EXE <REPO_ROOT>\chatbot\notify_from_file.py <pattern.json>`,

which delegates to `chatbot.trigger.notify_pattern` — that starts the FastAPI chat
server if it isn't running, POSTs the pattern to `/api/pattern`, and opens the browser
at `http://localhost:5000`. `REPO_ROOT` and `PYTHON_EXE` come from the `.env` written
by `setup_revit_env.py`; if they're missing, the pattern is still saved to disk and a
note is written to `_diag.txt`, but the assistant won't auto-launch.

> The add-in is **headless** (no WPF). The BIM Assistant is the browser chat at
> `:5000`, so there is no in-Revit toast/panel.
