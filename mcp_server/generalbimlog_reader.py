"""
Adapter: generalBIMlog `RevitLogger` output  ->  pipeline `ActionRecord` stream.

The pipeline's detector consumes an ACTION stream (Place / one-param SetParam /
Tag / Delete keyed by element_id). The generalBIMlog logger instead writes a
`ProjectSchema` JSON per project (one file per `{projectGUID}.json`) in an
ELEMENT-EVENT model:

    { projectGUID, projectName, renames[], sessions: [
        { sessionId, userName, startTime, endTime, events: [
            { eventId, timestamp, eventType: CREATED|REVISED|DELETED,
              element: { general:{elementId,category,family,type},
                         parameters:{category,family,type,instance:{Built-In,Custom}},
                         geometry, annotation:{annotationKind,taggedElementIds,text,...} } }
        ] } ] }

generalBIMlog is STATE-FREE: every REVISED event carries the element's FULL
parameter snapshot, not a delta. So this adapter is stateful — it tracks each
element's last-seen instance params and diffs consecutive snapshots to recover
per-parameter SetParam actions.

Mapping
    CREATED (model element)      -> Place   (+ seed param baseline, no SetParam)
    CREATED (annotation, Tag)    -> Tag     (tagged_element_id from taggedElementIds)
    REVISED (model element)      -> SetParam per CHANGED user-editable instance param
    DELETED                      -> Delete  (detector ignores it in assembly)
    CREATED/REVISED Text/Dim     -> skipped (no slot in the Place/SetParam/Tag model)

REVISED -> SetParam filter (the "user-editable params only" policy)
    Revit fires REVISED for internal recomputation too (auto-joins recompute
    length/area, set phase/IFC GUID, toggle attach flags). Geometry lives in the
    `category`/`geometry` sections — we diff only `instance` params, which already
    avoids most of it. The remaining instance-level auto params are excluded by a
    deny-list; user text/enum fields (String/Integer) are included by default;
    a small allow-list re-includes meaningful numerics. All three lists are module
    constants — heuristic and meant to be tuned against real logs (documented
    limitation: a snapshot diff cannot *prove* a change was user-initiated).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from shared.schemas import ActionRecord

# ── Where generalBIMlog writes (per LogPathManager.cs) ────────────────────────
#   %APPDATA%\Autodesk\Revit\Addins\<ver>\RevitLogger\Logs\eventlog\*.json
# A custom logs folder is supported via the GENERALBIMLOG_DIR override (point it
# at the folder that *contains* the eventlog/ subdir, or directly at eventlog/).

def eventlog_dirs() -> list[Path]:
    """All generalBIMlog eventlog directories to read (override + every Revit ver)."""
    override = os.environ.get("GENERALBIMLOG_DIR")
    if override:
        p = Path(override)
        cands = [p, p / "eventlog", p / "Logs" / "eventlog"]
        return [c for c in cands if c.exists()] or [p]
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    base = Path(appdata) / "Autodesk" / "Revit" / "Addins"
    return sorted(base.glob("*/RevitLogger/Logs/eventlog")) if base.exists() else []


# ── REVISED -> SetParam filter (heuristic, tunable) ───────────────────────────

# Instance params considered user-editable by storage type (covers Mark, Comments,
# yes/no + enum integers, host/level ElementId refs).
_EDITABLE_STORAGE = {"String", "Integer", "ElementId"}

# Re-include these keys even when their storage type is Double (numerics users set).
_ALLOW_KEYS = {
    "WALL_USER_HEIGHT_PARAM", "WALL_BASE_OFFSET", "WALL_TOP_OFFSET",
    "INSTANCE_SILL_HEIGHT_PARAM", "INSTANCE_HEAD_HEIGHT_PARAM",
    "FAMILY_WIDTH_PARAM", "FAMILY_HEIGHT_PARAM",
    "GENERIC_WIDTH", "GENERIC_HEIGHT", "DOOR_WIDTH", "DOOR_HEIGHT",
    "FURNITURE_WIDTH", "FURNITURE_HEIGHT",
}

# Always exclude — set by Revit internals / auto-join / derived, never a user edit.
_DENY_KEYS = {
    "IFC_GUID", "IFC_EXPORT_ELEMENT", "IFC_EXPORT_ELEMENT_AS",
    "PHASE_CREATED", "PHASE_DEMOLISHED",
    "ELEM_PARTITION_PARAM", "DESIGN_OPTION_ID",
    "ELEM_TYPE_PARAM", "ELEM_FAMILY_PARAM", "ELEM_FAMILY_AND_TYPE_PARAM",
    "ELEM_CATEGORY_PARAM", "ELEM_CATEGORY_PARAM_MT",
    "WALL_BOTTOM_IS_ATTACHED", "WALL_TOP_IS_ATTACHED",
    "WALL_BOTTOM_EXTENSION_DIST_PARAM", "WALL_TOP_EXTENSION_DIST_PARAM",
}
_DENY_SUFFIXES = ("_COMPUTED", "_AREA", "_VOLUME", "_LENGTH", "_PERIMETER", "_ELEVATION")

# Friendly names for common Built-In params (readability for agents/labels).
_FRIENDLY = {
    "ALL_MODEL_MARK": "Mark",
    "ALL_MODEL_INSTANCE_COMMENTS": "Comments",
    "ALL_MODEL_TYPE_COMMENTS": "Type Comments",
    "WALL_BASE_CONSTRAINT": "Base Constraint",
    "WALL_HEIGHT_TYPE": "Top Constraint",
    "WALL_USER_HEIGHT_PARAM": "Unconnected Height",
    "WALL_BASE_OFFSET": "Base Offset",
    "INSTANCE_SILL_HEIGHT_PARAM": "Sill Height",
    "DOOR_NUMBER": "Mark",
}


def _is_user_editable(key: str, storage_type: str) -> bool:
    if key in _DENY_KEYS or key.endswith(_DENY_SUFFIXES):
        return False
    if key in _ALLOW_KEYS:
        return True
    return storage_type in _EDITABLE_STORAGE


def _friendly(key: str) -> str:
    return _FRIENDLY.get(key, key)


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_unix(ts: str) -> float:
    """generalBIMlog timestamps are 'yyyy-MM-dd HH:mm:ss' (local)."""
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return 0.0


def _strip_ost(category: str | None) -> str:
    c = category or ""
    return c[4:] if c.startswith("OST_") else c


# Documentation / view-like categories (post-_strip_ost): a CREATED element in one of these is a
# 'Create' action (operation_class View), not a model 'Place' — Track D (beyond instantiation).
_VIEWLIKE_CATS = {"Views", "Sheets", "Viewports", "Schedules", "ScheduleGraphics", "Legends"}


def _to_int_id(elementId) -> int:
    try:
        return int(elementId)
    except (ValueError, TypeError):
        return 0


# Built-In params that carry the element's LEVEL (read for ActionRecord.level_name so per-level
# conventions — e.g. Mark numbered per floor — can be induced from the logs, offline). The logger
# records these as ElementId params whose ValueString is the readable level name (e.g. "L1").
_LEVEL_KEYS = ("FAMILY_LEVEL_PARAM", "WALL_BASE_CONSTRAINT", "SCHEDULE_LEVEL_PARAM",
               "INSTANCE_REFERENCE_LEVEL_PARAM", "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM",
               "LEVEL_PARAM", "ROOM_LEVEL_ID")


def _level_name(parameters: dict | None) -> str:
    """The element's level name from its instance params (first level key present), else ''."""
    inst = ((parameters or {}).get("instance")) or {}
    for grp in ("Built-In", "Custom"):
        g = inst.get(grp) or {}
        if not isinstance(g, dict):
            continue
        for key in _LEVEL_KEYS:
            pdata = g.get(key)
            if isinstance(pdata, dict):
                vs = pdata.get("ValueString")
                if vs:
                    return str(vs)
    return ""


def _flatten_instance(parameters: dict | None) -> dict[str, tuple[str, str]]:
    """instance params -> {key: (storage_type, value_string)} across Built-In+Custom."""
    inst = ((parameters or {}).get("instance")) or {}
    out: dict[str, tuple[str, str]] = {}
    for group in ("Built-In", "Custom"):
        g = inst.get(group) or {}
        if not isinstance(g, dict):
            continue
        for key, pdata in g.items():
            if isinstance(pdata, dict):
                st = pdata.get("StorageType", "") or ""
                vs = pdata.get("ValueString")
                if vs is None:
                    v = pdata.get("Value")
                    vs = "" if v is None else str(v)
            else:
                st, vs = "", ("" if pdata is None else str(pdata))
            out[key] = (st, str(vs))
    return out


# ── conversion ────────────────────────────────────────────────────────────────

def project_to_action_records(project: dict) -> list[ActionRecord]:
    """Convert one parsed generalBIMlog ProjectSchema dict into ActionRecords."""
    records: list[ActionRecord] = []
    # last-seen instance param snapshot per element_id (for REVISED diffing)
    baseline: dict[int, dict[str, tuple[str, str]]] = {}

    for session in project.get("sessions", []) or []:
        sid = session.get("sessionId", "") or ""
        for ev in session.get("events", []) or []:
            etype = ev.get("eventType")
            elem = ev.get("element") or {}
            gen = elem.get("general") or {}
            eid = _to_int_id(gen.get("elementId"))
            ts = _to_unix(ev.get("timestamp", ""))
            cat = _strip_ost(gen.get("category"))
            fam = gen.get("family") or ""
            typ = gen.get("type") or ""
            ann = elem.get("annotation")

            common = dict(
                session_id=sid, timestamp_unix=ts, timestamp_utc=ev.get("timestamp", ""),
                element_id=eid, element_category=cat, family_name=fam, type_name=typ,
                level_name=_level_name(elem.get("parameters")),
            )

            if etype == "DELETED":
                records.append(ActionRecord(action_type="Delete", operation_class="Model", **common))
                baseline.pop(eid, None)
                continue

            is_tag = bool(ann) and ann.get("annotationKind") == "Tag"

            if etype == "CREATED":
                if is_tag:
                    tagged = ann.get("taggedElementIds") or []
                    records.append(ActionRecord(
                        action_type="Tag", operation_class="Annotation",
                        tag_family_name=fam,
                        tagged_element_id=_to_int_id(tagged[0]) if tagged else None,
                        **common,
                    ))
                elif ann is not None:
                    # Text / Dimension (non-tag annotation) → Create (Track D: beyond instantiation).
                    records.append(ActionRecord(action_type="Create", operation_class="Annotation", **common))
                    baseline[eid] = _flatten_instance(elem.get("parameters"))
                elif cat in _VIEWLIKE_CATS:
                    # Documentation elements (views/sheets/viewports/schedules) → Create with a param
                    # baseline, so a rename / template assignment diffs into SetParams — this is what
                    # makes 'duplicate view → rename → apply template → sheet it' a learnable routine.
                    records.append(ActionRecord(action_type="Create", operation_class="View", **common))
                    baseline[eid] = _flatten_instance(elem.get("parameters"))
                else:
                    records.append(ActionRecord(action_type="Place", operation_class="Model", **common))
                    baseline[eid] = _flatten_instance(elem.get("parameters"))

            elif etype == "REVISED":
                if is_tag or ann is not None:
                    continue  # annotation edits aren't routine SetParams
                new = _flatten_instance(elem.get("parameters"))
                old = baseline.get(eid)
                if old is None:
                    baseline[eid] = new  # first time we see it — seed, can't diff
                    continue
                for key, (st, vs) in new.items():
                    old_st, old_vs = old.get(key, ("", None)) if key in old else ("", None)
                    if vs == old_vs:
                        continue
                    if not _is_user_editable(key, st):
                        continue
                    records.append(ActionRecord(
                        action_type="SetParam", operation_class="Parameter",
                        param_name=_friendly(key), param_storage_type=st,
                        param_value_before=old_vs, param_value_after=vs,
                        **common,
                    ))
                baseline[eid] = new

    return records


def load_action_records(dirs: list[Path] | None = None) -> list[ActionRecord]:
    """Read every generalBIMlog ProjectSchema file and return one ActionRecord stream."""
    import sys

    out: list[ActionRecord] = []
    for d in (dirs if dirs is not None else eventlog_dirs()):
        for f in sorted(Path(d).glob("*.json")):
            try:
                project = json.loads(f.read_text(encoding="utf-8-sig"))
                out.extend(project_to_action_records(project))
            except Exception as e:  # keep going past a bad file
                print(f"  [generalbimlog_reader] skip {f.name}: {e}", file=sys.stderr)
    return out


# ── CLI: dump converted actions for one file or the default dirs ──────────────

if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args:
        project = json.loads(Path(args[0]).read_text(encoding="utf-8-sig"))
        recs = project_to_action_records(project)
    else:
        recs = load_action_records()

    from collections import Counter
    print(f"converted {len(recs)} ActionRecords")
    print("by action_type:", dict(Counter(r.action_type for r in recs)))
    for r in recs[:40]:
        extra = (f" {r.param_name}={r.param_value_after!r}" if r.action_type == "SetParam"
                 else f" -> tagged {r.tagged_element_id}" if r.action_type == "Tag"
                 else f" {r.family_name}")
        print(f"  {r.element_id:>9}  {r.action_type:<9}{extra}")
