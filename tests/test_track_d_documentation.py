"""Track D — beyond element instantiation: documentation routines are detectable.

Proves the pipeline widening end-to-end (no Revit, no disk):
  • CREATED view/sheet (OST_Views/OST_Sheets)        -> Create (operation_class View), baseline seeded
  • REVISED view with a VIEW_NAME change (rename)    -> SetParam on the SAME element id
  • CREATED non-tag annotation (dimension)           -> Create (Annotation), no longer skipped
  • 3x 'duplicate view -> rename -> template' chain  -> ONE Create-rooted candidate routine
  • existing Place-rooted behavior untouched (regression covered by the 212-test suite)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from detector import make_detector
from detector._common import token
from mcp_server.generalbimlog_reader import project_to_action_records


def _bip(storage, value):
    return {"StorageType": storage, "Value": value, "ValueString": str(value)}


def _view_created(eid, ts, name):
    return {
        "eventId": 0, "timestamp": ts, "eventType": "CREATED",
        "element": {
            "general": {"elementId": str(eid), "category": "OST_Views",
                        "family": "", "type": "Floor Plan"},
            "parameters": {"instance": {"Built-In": {
                "VIEW_NAME": _bip("String", name),
                "VIEW_TEMPLATE": _bip("ElementId", -1),
            }}},
        },
    }


def _view_revised(eid, ts, name=None, template=None):
    params = {"VIEW_NAME": _bip("String", name) if name else _bip("String", f"unchanged-{eid}"),
              "VIEW_TEMPLATE": _bip("ElementId", template if template is not None else -1)}
    return {
        "eventId": 0, "timestamp": ts, "eventType": "REVISED",
        "element": {
            "general": {"elementId": str(eid), "category": "OST_Views",
                        "family": "", "type": "Floor Plan"},
            "parameters": {"instance": {"Built-In": params}},
        },
    }


def _sheet_created(eid, ts):
    return {
        "eventId": 0, "timestamp": ts, "eventType": "CREATED",
        "element": {
            "general": {"elementId": str(eid), "category": "OST_Sheets",
                        "family": "", "type": ""},
            "parameters": {"instance": {"Built-In": {
                "SHEET_NUMBER": _bip("String", "A-101"),
            }}},
        },
    }


def _dimension_created(eid, ts):
    return {
        "eventId": 0, "timestamp": ts, "eventType": "CREATED",
        "element": {
            "general": {"elementId": str(eid), "category": "OST_Dimensions",
                        "family": "Linear Dimension Style", "type": "2.5mm Arial"},
            "annotation": {"annotationKind": "Dimension"},
            "parameters": {},
        },
    }


def _doc_project():
    """3 repetitions of: duplicate view (Create) -> rename (SetParam) -> set template (SetParam)."""
    events = []
    t = 0
    for i, eid in enumerate((5001, 5002, 5003)):
        base = f"2026-06-16 09:0{i}:0"
        events.append(_view_created(eid, base + "1", f"Copy of Plan {i}"))
        events.append(_view_revised(eid, base + "3", name=f"100_EG_Plan_{i:02}"))
        events.append(_view_revised(eid, base + "5", name=f"100_EG_Plan_{i:02}", template=777))
    return {"projectGUID": "g", "projectName": "p",
            "sessions": [{"sessionId": "s1", "events": events}]}


def test_view_created_becomes_create_with_view_class():
    recs = project_to_action_records(_doc_project())
    creates = [r for r in recs if r.action_type == "Create"]
    assert len(creates) == 3
    assert all(r.operation_class == "View" for r in creates)
    assert token(creates[0]) == "Create:Floor Plan"      # type is the discriminating key


def test_view_rename_and_template_become_setparams():
    recs = project_to_action_records(_doc_project())
    sets = [r for r in recs if r.action_type == "SetParam"]
    # per repetition: one VIEW_NAME change + one VIEW_TEMPLATE change
    assert len(sets) == 6
    assert {r.element_id for r in sets} == {5001, 5002, 5003}


def test_sheet_and_dimension_become_create_not_skipped():
    proj = {"projectGUID": "g", "projectName": "p", "sessions": [{"sessionId": "s1", "events": [
        _sheet_created(6001, "2026-06-16 10:00:01"),
        _dimension_created(6002, "2026-06-16 10:00:02"),
    ]}]}
    recs = project_to_action_records(proj)
    assert [r.action_type for r in recs] == ["Create", "Create"]
    assert recs[0].operation_class == "View"        # sheet
    assert recs[1].operation_class == "Annotation"  # dimension (previously skipped)


def test_detector_recovers_documentation_routine():
    recs = project_to_action_records(_doc_project())
    cands = make_detector("v2").detect(recs, session_id="track_d")
    assert len(cands) == 1, f"expected 1 documentation routine, got {len(cands)}"
    assert cands[0].support == 3
    # the routine is Create-rooted (not Place-rooted)
    tokens = [token(r) for r in recs if r.element_id == 5001]
    assert tokens[0] == "Create:Floor Plan"
    assert all(t.startswith("SetParam:") for t in tokens[1:])
