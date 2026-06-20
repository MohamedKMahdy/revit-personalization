# Revit Add-in Documentation
## RevitLogger — BIM Authoring Log Capture

> ⚠ **Historical / retired.** This document describes the original **in-repo**
> `revit_addin/` logger (a Revit 2027-era prototype). That add-in is **retired**: the
> pipeline's live log source is now the **`generalBIMlog`** `RevitLogger` add-in (Revit
> 2025/2026), whose `ProjectSchema` output is adapted by
> [`mcp_server/generalbimlog_reader.py`](../mcp_server/generalbimlog_reader.py). The
> semantic-logging rationale below (the Jang & Lee schema and which fields are captured and
> why) still explains *what* the pipeline records, but the implementation specifics here are
> kept for historical reference only. For the current architecture see
> [`PROJECT_OVERVIEW.md`](../PROJECT_OVERVIEW.md) and [`docs/architecture.md`](architecture.md).

---

## 1. Purpose

RevitLogger is a Revit 2027 C# add-in that silently captures a user's BIM authoring actions and writes them to structured log files on disk. It is the data-collection layer of the thesis system.

Its design follows the enhanced BIM logging schema proposed by:
- **Jang & Lee (2023)** *"An enhanced BIM log schema for reproducibility analysis"* arXiv:2305.18032
- **Jang et al. (2023)** *"Lexicon-based BIM log analysis for identifying design intent"* AEI 57, 102079

The key insight from these papers is that raw Revit journal files are insufficient for workflow analysis because they lack semantic context (which family was placed, which parameter was changed, what the value was before and after). RevitLogger addresses all of these gaps.

---

## 2. Add-in Architecture

```
Revit 2027 process
│
└── App.cs  (IExternalApplication)
      │
      │  subscribes to application-level events
      │
      ├── DocumentChanged  ──▶  ActionCapture.ProcessEvent()
      ├── DocumentOpened   ──▶  StartSession()
      ├── DocumentCreated  ──▶  StartSession()
      └── DocumentClosing  ──▶  EndSession()

Per-document session:
  ActionCapture   ─── reads Revit API, creates ActionRecords
  ElementSnapshot ─── caches parameter values, computes diffs
  LogWriter       ─── async queue → JSONL file on disk
```

### 2.1 Session Lifecycle

When a project document is opened:
1. A unique `session_id` is generated (`sess_YYYYMMDDHHMMSS`)
2. A `LogWriter` is created — this opens the session file and starts the background write loop
3. A `SessionInfo` record is written as line 1 of the file
4. An `ActionCapture` instance is registered for that document

When the document closes (or Revit shuts down):
1. A `session_end` record is enqueued
2. The write loop drains the queue, writes the final record, and closes the file

Multiple documents can be open simultaneously; each gets its own session.

### 2.2 Document Key

Documents are identified by their file path (lowercased). For unsaved documents, `"unsaved::" + doc.Title` is used as the key. This ensures the same document is consistently tracked across `DocumentOpened` and `DocumentChanged` events within the same Revit session.

---

## 3. Event Handling

### 3.1 DocumentChanged

This is the primary event. It fires once per **committed Revit transaction** — that is, after the user completes an atomic action (not during dragging or live preview). This is the correct granularity for workflow analysis.

For each transaction, `ActionCapture.ProcessEvent()`:
1. Creates a `RecordContext` shared by all records in this transaction:
   - `transaction_id`: a random 12-hex-char identifier
   - `transaction_name`: from `e.GetTransactionNames()` — the undo-stack label (e.g. `"Door"`, `"Modify element attributes"`, `"Tag"`)
   - `timestamp_utc` / `timestamp_unix`
   - Active view context (`view_id`, `view_name`, `view_type`)

2. Iterates `GetAddedElementIds()` → calls `HandleAdded()`
3. Iterates `GetModifiedElementIds()` → calls `HandleModified()`
4. Iterates `GetDeletedElementIds()` → removes from `ElementSnapshot` cache

### 3.2 What Triggers a Record

| User action | Revit event | Record type emitted |
|-------------|------------|---------------------|
| Place a door / window / column / etc. | `AddedElementIds` contains a `FamilyInstance` in an authoring category | `Place` |
| Change a parameter value on an element | `ModifiedElementIds` contains a tracked `FamilyInstance`, and the parameter value differs from the cached snapshot | `SetParam` |
| Add a tag to an element | `AddedElementIds` contains an `IndependentTag` | `Tag` |
| Delete an element | `DeletedElementIds` | *no record emitted* — element removed from snapshot cache |
| Move an element | `ModifiedElementIds` — but no parameter values changed | *no record* — position change not captured (by design) |
| Drag a tag leader | `ModifiedElementIds` contains a tag | *no record* — tag modifications explicitly suppressed (noise) |

### 3.3 Authoring Category Filter

Only elements in these Revit categories are tracked:

```
Doors              Windows
Structural Columns  Structural Framing   Structural Foundations
Furniture           Furniture Systems    Casework
Mechanical Equipment  Plumbing Fixtures  Electrical Equipment
Lighting Fixtures   Specialty Equipment  Generic Models
Walls               Floors              Roofs
Ceilings            Columns             Stairs              Railings
```

**Why these categories?** They correspond to the *component placement* actions that Jang & Lee (2023) identify as the dominant action type in design-phase BIM authoring workflows. View elements, annotation objects (except tags), and system families like duct runs are excluded because they are not part of the "Custom Element Instantiation" loop the thesis targets.

---

## 4. Data Collected: Every Field Explained

### 4.1 Session Start Record

Written as line 1 of every `.jsonl` file. Identifies the recording context.

```json
{
  "record_type": "session_start",
  "schema_version": "2.0",
  "session_id": "sess_20260523035731",
  "timestamp_utc": "2026-05-23T03:57:31.935Z",
  "revit_version": "Autodesk Revit 2027",
  "document_hash": "69003c58b5f0",
  "document_title": "Snowdon Towers Sample Architectural"
}
```

| Field | Value example | Why collected |
|-------|--------------|---------------|
| `record_type` | `"session_start"` | Allows parsers to distinguish metadata lines from action lines |
| `schema_version` | `"2.0"` | Forward-compatibility: future schema changes are versioned |
| `session_id` | `"sess_20260523035731"` | Links all records in this file; referenced by `CandidateRoutine` objects |
| `timestamp_utc` | ISO 8601 string | Absolute time anchor for the session |
| `revit_version` | `"Autodesk Revit 2027"` | Documents the tool version; relevant if API behaviour differs across versions |
| `document_hash` | First 12 hex chars of SHA1(path) | Identifies which project was open **without exposing the file path** (privacy — thesis §3.1 gap 3) |
| `document_title` | Filename without extension | Human-readable project identifier for log inspection |

### 4.2 ActionRecord — Place

Emitted when a family instance is placed in an authoring category.

```json
{
  "schema_version": "2.0",
  "event_id": "cd2b7faa522c",
  "session_id": "sess_20260523035731",
  "transaction_id": "1e9d18e5f8c6",
  "transaction_name": "Door",
  "timestamp_utc": "2026-05-23T04:06:04.268Z",
  "timestamp_unix": 1779509164.268,
  "action_type": "Place",
  "operation_class": "Model",
  "element_id": 3327603,
  "element_category": "Doors",
  "family_name": "Door-Passage-Single-Full_Lite",
  "type_name": "36\" x 84\"",
  "level_name": "L1 - Block 35",
  "phase_name": "New Construction",
  "host_category": "Walls",
  "view_id": 1350581,
  "view_name": "L1",
  "view_type": "FloorPlan"
}
```

| Field | Source in Revit API | Why collected |
|-------|--------------------|----|
| `event_id` | Generated UUID | Unique record identifier; deduplication if the file is re-read |
| `session_id` | From LogWriter | Groups all records in this session; used by log_reader to scope episode detection |
| `transaction_id` | Generated per `DocumentChangedEventArgs` | **Groups all records from one Revit transaction** — the atomic unit of authoring. Jang & Lee (2023) §3.2 identify this as essential for reproducibility: "a single user intent may generate multiple element modifications" |
| `transaction_name` | `e.GetTransactionNames()` | The undo-stack label (e.g. `"Door"`, `"Place Component"`) — provides semantic intent without requiring NLP; directly from Revit's internal labelling system |
| `timestamp_utc` / `timestamp_unix` | `DateTime.UtcNow` | Dual format: UTC string for human readability; Unix epoch float for sorting and time-gap analysis |
| `action_type` | Determined by code logic | `"Place"` — the primary action taxonomy from Jang et al. (2023) AEI lexicon |
| `operation_class` | Enum: `Model` | **Jang et al. (2023) AEI taxonomy**: Model / Parameter / Annotation / View. Enables filtering by class without string matching |
| `element_id` | `fi.Id.Value` (long cast to int) | **The key for episode grouping**: all SetParam and Tag records referencing the same element are linked by this ID |
| `element_category` | `fi.Category.Name` | Category string (e.g. `"Doors"`) — used as the first component of the episode signature for routine detection |
| `family_name` | `fi.Symbol.Family.Name` | **The most important field for routine detection** — identifies which family was used. Two placements of `"Door-Passage-Single-Full_Lite"` are instances of the same routine; one of `"M_Bifold-2 Panel"` is a different routine |
| `type_name` | `fi.Symbol.Name` | The specific type within the family (e.g. `"36\" x 84\""`) — used to detect if the user always picks the same type (constant) or varies it |
| `level_name` | `doc.GetElement(fi.LevelId).Name` | **Spatial context**: which floor/level the element was placed on. Jang & Lee (2023) note that many routines are level-specific (e.g. "all doors on L1 get tagged with L1 Door Tag") |
| `phase_name` | `fi.get_Parameter(BuiltInParameter.PHASE_CREATED).AsValueString()` | **Temporal project context**: "New Construction" vs "Demolition". Required for full reproducibility per Jang & Lee (2023) §4.1 |
| `host_category` | `fi.Host?.Category?.Name` | For hosted elements (doors, windows): records what they are hosted in (e.g. `"Walls"`). Distinguishes wall-hosted doors from curtain-wall-hosted doors |
| `view_id` | `doc.ActiveView.Id.Value` | Links the action to the specific view it was performed in — a required field in the Jang & Lee schema |
| `view_name` | `doc.ActiveView.Name` | Human-readable view identifier (e.g. `"L1"`, `"South Elevation"`) |
| `view_type` | `doc.ActiveView.ViewType.ToString()` | `FloorPlan` / `Elevation` / `Section` / `3D` — critical for precondition detection: certain actions only make sense in certain view types |

### 4.3 ActionRecord — SetParam

Emitted when a parameter value changes on a tracked family instance. Uses `ElementSnapshot` to produce a before/after diff.

```json
{
  "action_type": "SetParam",
  "operation_class": "Parameter",
  "element_id": 3327603,
  "element_category": "Doors",
  "family_name": "Door-Passage-Single-Full_Lite",
  "type_name": "36\" x 84\"",
  "level_name": "L1 - Block 35",
  "view_id": 1350581,
  "view_name": "L1",
  "view_type": "FloorPlan",
  "param_name": "Mark",
  "param_storage_type": "String",
  "param_value_before": "3C09",
  "param_value_after": "DD1",
  "transaction_id": "5cb666586dc8",
  "transaction_name": "Modify element attributes"
}
```

SetParam-specific fields:

| Field | Source | Why collected |
|-------|--------|---------------|
| `param_name` | `p.Definition.Name` | The parameter being changed (e.g. `"Mark"`, `"Fire Rating"`, `"Width"`) — used by the Pattern Agent to identify which parameters are part of the routine |
| `param_storage_type` | `p.StorageType.ToString()` | `String` / `Integer` / `Double` — tells the Pattern Agent and execution layer how to interpret and set the value |
| `param_value_before` | From `ElementSnapshot` cache (value at last snapshot) | **Required for reproducibility** per Jang & Lee (2023) §3.3: "before/after diffs enable audit trails and support undo-aware replay" |
| `param_value_after` | Current `p.AsString()` / `p.AsInteger()` / `Math.Round(p.AsDouble() * 304.8, 0)` | The new value. The Pattern Agent uses this across k examples to determine if the value is constant (same every time) or variable (different each time → prompt user) |

**Unit conversion note:** Double parameters are stored internally in Revit in decimal feet. RevitLogger converts to millimetres (`× 304.8`) rounded to the nearest mm before logging. This matches the units designers expect and makes values interpretable without Revit unit metadata.

**Parameter filtering** (`ElementSnapshot.ShouldTrack`):

Parameters are excluded if:
- They are read-only (`p.IsReadOnly == true`)
- Storage type is `None` or `ElementId` (non-primitive, not settable via simple assignment)
- Name is in the exclusion list:

```
Area, Volume, Perimeter          (computed geometry — not user-authored)
Phase Created, Phase Demolished  (set automatically by Revit)
Work Plane, Host, Workset        (structural/collaborative metadata)
Design Option                    (project organisation, not design intent)
Image                            (not a meaningful routine parameter)
Moves With Nearby Elements       (internal Revit behaviour flag)
Room: Name, Room: Number         (room-computed, not user-set on the element)
Space: Name, Space: Number       (same)
Family, Family and Type          (read-only type descriptors)
```

These are excluded because they change automatically as side-effects of other operations — not because the user explicitly set them. Including them would produce false SetParam records and confuse the Pattern Agent.

### 4.4 ActionRecord — Tag

Emitted when an `IndependentTag` is added to a document. Tags are captured **only on add** (not on modify, which would capture leader repositioning as noise).

```json
{
  "action_type": "Tag",
  "operation_class": "Annotation",
  "element_id": 3327683,
  "element_category": "Door Tags",
  "family_name": "Door Tag",
  "type_name": "Door Tag",
  "level_name": "",
  "view_id": 1350581,
  "view_name": "L1",
  "view_type": "FloorPlan",
  "tag_family_name": "Door Tag",
  "tagged_element_id": 3327603,
  "transaction_id": "1d461876bdb8",
  "transaction_name": "Tag"
}
```

Tag-specific fields:

| Field | Source | Why collected |
|-------|--------|---------------|
| `tag_family_name` | `(doc.GetElement(tag.GetTypeId()) as FamilySymbol).Family.Name` | Which tag family was used — the routine might always use `"Door Tag"` (constant) or vary by door type |
| `tagged_element_id` | `doc.GetElement(tag.GetTaggedReferences()[0]).Id.Value` | **Critical for episode linkage**: allows `log_reader` to attach the Tag record to the element episode it belongs to. Without this, tags would be orphaned records disconnected from the Place+SetParam sequence |

**Why tags are important for the thesis:** The "Custom Element Instantiation" loop always ends with annotation (tagging). The tag record closes the episode. Without it, the routine `Place → SetParam → Tag` would only be detected as `Place → SetParam` and the automation shortcut would fail to reproduce the full workflow.

**Revit 2027 API note:** Two API methods used for tagging were removed in Revit 2027 and required migration:
- `IndependentTag.GetTaggedLocalElement()` → replaced by `tag.GetTaggedReferences()` which returns `IList<Reference>`, then `doc.GetElement(refs[0])`
- `IndependentTag.Symbol` → replaced by `doc.GetElement(tag.GetTypeId()) as FamilySymbol`

### 4.5 Session End Record

Written when the document closes or Revit shuts down.

```json
{
  "record_type": "session_end",
  "session_id": "sess_20260523035731",
  "timestamp_utc": "2026-05-23T04:15:00.123Z"
}
```

Signals that the file is complete. Parsers that encounter a file without a `session_end` record treat it as an incomplete session (Revit crashed or was force-killed) and process it with a warning.

---

## 5. The ElementSnapshot Mechanism

`ElementSnapshot` is what makes SetParam detection possible. The problem: when `DocumentChanged` fires for a modification, Revit reports *which elements changed* but not *which parameters changed* or *what the values were before*.

**Solution:** snapshot the parameter values immediately after a Place event, then diff against current values on every subsequent modification.

```
Place event fires for element E
  → ElementSnapshot.Snapshot(fi)
       stores { "Mark": "3C09", "Width": 914, "Fire Rating": "" }
       for element E

User changes Mark from "3C09" to "DD1"
  → DocumentChanged fires for element E (ModifiedElementIds)
  → ElementSnapshot.GetChanges(fi)
       current values: { "Mark": "DD1", "Width": 914, "Fire Rating": "" }
       diff vs. cached: Mark changed
       → emits SetParam(Mark, before="3C09", after="DD1")
       → updates cache to current values (eager update)
```

The cache is keyed by `element_id` (as `long`, using `fi.Id.Value`). On deletion, `Remove(id.Value)` is called to prevent stale cache entries accumulating over a long session.

---

## 6. Log File Format

**Location:** `%LOCALAPPDATA%\RevitPersonalization\logs\`  
**Filename:** `session_YYYYMMDD_HHmmss_<docHash>.jsonl`  
**Format:** JSONL (JSON Lines) — one JSON object per line, UTF-8 without BOM

```
{"record_type":"session_start", ...}          ← always line 1
{"action_type":"Place", ...}                  ← action records (any order)
{"action_type":"SetParam", ...}
{"action_type":"Tag", ...}
{"action_type":"Place", ...}
...
{"record_type":"session_end", ...}            ← always last line (if complete)
```

**Why JSONL?** Append-only; each line is independently parseable; robust to incomplete writes (if Revit crashes, all lines before the crash are valid); compatible with streaming log analysis tools.

---

## 7. Revit 2027 API Changes

The add-in was specifically designed for Revit 2027. Three breaking API changes from earlier versions required explicit handling:

| Removed API | Replacement used | Reason for change |
|-------------|-----------------|-------------------|
| `ElementId.IntegerValue` (int) | `ElementId.Value` (long) | Element IDs in Revit 2027 expanded to 64-bit to support large models |
| `IndependentTag.GetTaggedLocalElement()` | `tag.GetTaggedReferences()[0]` then `doc.GetElement(ref)` | The old method did not support multi-reference tags; the new API is more general |
| `IndependentTag.Symbol` | `doc.GetElement(tag.GetTypeId()) as FamilySymbol` | `Symbol` was a convenience property that bypassed the element type system; removed for consistency |
| `LabelUtils.GetLabelFor(ForgeTypeId)` | Not used; name-based blacklist instead | The `ForgeTypeId` overload was removed from the public API; group-label filtering is replaced by a parameter name exclusion list |

---

## 8. Deployment

**Build:**
```powershell
cd RevitLogger
dotnet build -c Release
```

**Deploy** (Revit must be closed):
```powershell
.\deploy.ps1
# or manually:
copy bin\Release\net10.0-windows\RevitLogger.dll ^
     %APPDATA%\Autodesk\Revit\Addins\2027\RevitLogger.dll
```

The `.addin` manifest file is already in `%APPDATA%\Autodesk\Revit\Addins\2027\RevitLogger.addin` and does not change between builds.

**Verify it loaded:** On Revit startup you will see a "Load Add-in" security dialog. Click "Always Load" to suppress it on future starts.

**Diagnostics:** If the add-in is loaded but no records appear, check:
```
%LOCALAPPDATA%\RevitPersonalization\logs\_diag.txt
```
This file traces every event handler call, session start/stop, and write-loop operation with millisecond timestamps.

If the write loop crashed, check:
```
%LOCALAPPDATA%\RevitPersonalization\logs\session_*.jsonl.error.txt
```

---

## 9. What Is NOT Collected

Understanding the boundaries of data collection is as important as understanding what is collected:

| Not collected | Reason |
|--------------|--------|
| Element geometry (coordinates, dimensions) | Not needed for routine detection; would require serialising Revit geometry objects |
| Document file path | Privacy: only a SHA-1 hash of the path is stored |
| User identity | No user account or machine ID is captured |
| Model contents not touched by the user | Only modified elements appear in `DocumentChangedEventArgs` |
| Undo / Redo operations | No special handling; if the user undoes a placement, the element is deleted and removed from the snapshot cache |
| View navigation (pan, zoom, orbit) | These do not trigger `DocumentChanged` |
| Selection changes | Selection is not a document modification |
| Parameter changes on non-authoring elements | The `IsAuthoring()` category filter excludes them |
| Position / rotation changes | Handled by `ModifiedElementIds` but produce no parameter diffs since position is not a trackable `Parameter` object |
