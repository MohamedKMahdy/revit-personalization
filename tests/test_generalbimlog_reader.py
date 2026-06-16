"""
Tests for the generalBIMlog -> ActionRecord adapter (mcp_server/generalbimlog_reader.py).

Builds a tiny in-memory generalBIMlog ProjectSchema (no Revit, no disk) exercising:
  • CREATED model element            -> Place
  • REVISED with a user edit (Mark)  -> SetParam(Mark)
  • REVISED with internal noise       -> NO SetParam (filtered)
  • CREATED annotation (Tag)         -> Tag with tagged_element_id
and confirms the detector recovers the place->setparam->tag routine.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.generalbimlog_reader import project_to_action_records
from detector import make_detector


# ── fixture builder ───────────────────────────────────────────────────────────

def _bip(storage, value):
    return {"StorageType": storage, "Value": value, "ValueString": str(value)}


def _wall_created(eid, ts):
    return {
        "eventId": 0, "timestamp": ts, "eventType": "CREATED",
        "element": {
            "general": {"elementId": str(eid), "category": "OST_Walls",
                        "family": "Basic Wall", "type": "Generic - 200mm"},
            "parameters": {"instance": {"Built-In": {
                "ALL_MODEL_MARK": _bip("String", ""),          # empty at create
                "IFC_GUID": _bip("String", f"guid-{eid}"),     # deny-listed
                "WALL_BASE_CONSTRAINT": _bip("ElementId", 100),
            }}},
        },
    }


def _wall_revised(eid, ts, mark, length):
    # Mark changes (real user edit) AND two noise params change (IFC + derived length).
    return {
        "eventId": 0, "timestamp": ts, "eventType": "REVISED",
        "element": {
            "general": {"elementId": str(eid), "category": "OST_Walls",
                        "family": "Basic Wall", "type": "Generic - 200mm"},
            "parameters": {"instance": {"Built-In": {
                "ALL_MODEL_MARK": _bip("String", mark),            # user edit -> SetParam
                "IFC_GUID": _bip("String", f"guid-{eid}-new"),     # denied key -> ignored
                "WALL_BASE_CONSTRAINT": _bip("ElementId", 100),    # unchanged
                "CURVE_ELEM_LENGTH": _bip("Double", length),       # _LENGTH suffix -> ignored
            }}},
        },
    }


def _tag_created(tag_eid, ts, tagged_eid, text):
    return {
        "eventId": 0, "timestamp": ts, "eventType": "CREATED",
        "element": {
            "general": {"elementId": str(tag_eid), "category": "OST_WallTags",
                        "family": "M_Wall Tag", "type": "8mm"},
            "annotation": {"annotationKind": "Tag", "taggedElementIds": [tagged_eid],
                           "text": text, "ownerViewId": "32"},
        },
    }


def _project():
    events = []
    sec = 0

    def ts():
        nonlocal sec
        sec += 5
        return f"2026-06-16 00:{sec // 60:02d}:{sec % 60:02d}"

    # three identical "place wall -> set Mark -> tag" routines, interleaved
    for i, wall in enumerate((1000, 1001, 1002)):
        tag = 2000 + i
        events.append(_wall_created(wall, ts()))
        events.append(_wall_revised(wall, ts(), mark=str(i + 1), length=5.0 + i))
        events.append(_tag_created(tag, ts(), wall, str(i + 1)))

    for n, ev in enumerate(events, 1):
        ev["eventId"] = n

    return {
        "projectGUID": "test-guid", "projectName": "Test", "renames": [],
        "sessions": [{"sessionId": "s1", "userName": "tester",
                      "startTime": "2026-06-16 00:00:00", "endTime": None,
                      "events": events}],
    }


# ── tests ─────────────────────────────────────────────────────────────────────

def test_event_decomposition():
    recs = project_to_action_records(_project())
    by_type = {}
    for r in recs:
        by_type.setdefault(r.action_type, []).append(r)

    assert len(by_type["Place"]) == 3
    assert len(by_type["Tag"]) == 3
    assert len(by_type["SetParam"]) == 3, "expected exactly one SetParam (Mark) per wall"


def test_only_mark_survives_the_filter():
    recs = project_to_action_records(_project())
    setparams = [r for r in recs if r.action_type == "SetParam"]
    # noise params (IFC_GUID changed, CURVE_ELEM_LENGTH appeared) must be filtered out
    assert {r.param_name for r in setparams} == {"Mark"}
    assert all(r.param_value_after in {"1", "2", "3"} for r in setparams)


def test_tag_links_to_its_wall():
    recs = project_to_action_records(_project())
    tags = [r for r in recs if r.action_type == "Tag"]
    assert {r.tagged_element_id for r in tags} == {1000, 1001, 1002}
    assert all(r.tag_family_name == "M_Wall Tag" for r in tags)


def test_detector_recovers_the_routine():
    recs = project_to_action_records(_project())
    cands = make_detector("v2").detect(recs, session_id="test")
    assert len(cands) == 1
    assert cands[0].support == 3
    # the routine must contain a Place, a Tag, and a SetParam
    sig = cands[0].action_signature
    assert "P" in sig and "T" in sig and "S" in sig


def test_revised_before_baseline_is_seeded_not_emitted():
    # a REVISED with no preceding CREATED can't be diffed -> seeds, emits nothing
    proj = {
        "sessions": [{"sessionId": "s", "events": [
            _wall_revised(9000, "2026-06-16 00:00:05", mark="X", length=1.0),
        ]}],
    }
    recs = project_to_action_records(proj)
    assert [r.action_type for r in recs] == []  # nothing emitted from an orphan REVISED
