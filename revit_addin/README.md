# revit_addin — Personalization Revit Add-in (observer)

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
        → PatternBridge    on a repeat, notifies the BIM Assistant panel (TCP :8080)
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
| `PatternBridge.cs` | Notifies the BIM Assistant panel over TCP when a routine repeats |
| `NotificationUI.xaml(.cs)` | ⚠️ Not currently wired — see note below |
| `ShortcutRunner.cs` | Retired stub — execution moved to `mcp-servers-for-revit` |

## Build & deploy

Revit 2025/2026/2027 all host **.NET 8** — the project targets `net8.0-windows`.
The Revit version only selects which API DLLs are referenced (default 2027):

```powershell
# build against your installed Revit (override if not 2027)
dotnet build revit_addin\RevitLogger.csproj -c Release -p:RevitVersion=2026

# copy the DLL into the Revit add-ins folder (close Revit first — DLL is locked while open)
.\deploy.ps1
```

The `.addin` manifest (`RevitLogger.addin`) deploys once to
`%APPDATA%\Autodesk\Revit\Addins\<version>\` and points at `RevitLogger.dll`.

Logs land in `%LOCALAPPDATA%\RevitPersonalization\logs\` (`session_*.jsonl`, plus
`_diag.txt` for troubleshooting).

## Known gaps (flagged during the move out of revit-personalization root)

- **`PatternBridge` needs `RevitWriteServer` running.** The "notify → BIM Assistant
  panel" flow sends `notify_pattern` to a TCP server on `localhost:8080` that was
  provided by the old `RevitWriteServer` add-in (removed from this repo). Detection
  and logging work without it; only the auto-panel popup depends on it.
- **`NotificationUI` is orphaned.** `App.cs` went fully automatic ("No toast") and
  nothing constructs `NotificationUI`; its `onLearn` delegate referenced an
  `App.LaunchOrchestrator` that no longer exists. Kept for the thesis UI story but
  not on any active code path — remove it or re-wire it before relying on it.
