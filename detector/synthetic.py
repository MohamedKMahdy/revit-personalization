"""
Synthetic action-log generator for detector testing (no Revit required).

Builds raw `ActionRecord` streams for the five scenarios the v0.2 detector must
handle. Every routine instance is Place → SetParam* → Tag, with realistic
element-id usage (the Tag carries its own id + tagged_element_id).

Run as a script to dump a sample interleaved session to a .jsonl file:
    python -m detector.synthetic > sample_session.jsonl
"""
from __future__ import annotations

import json

from shared.schemas import ActionRecord

# Distinct families/tags so "door" and "window" differ by family + tag + params,
# which is what makes scenario 1 (separate) and scenario 3 (together) consistent.
DOOR_FAMILY = "M_Single-Flush"
DOOR_TAG = "Door Tag"
DOOR_PARAMS = ("Mark", "Fire Rating", "Width", "Height")

WINDOW_FAMILY = "M_Fixed"
WINDOW_TAG = "Window Tag"
WINDOW_PARAMS = ("Mark", "Width", "Height")


# ── Record builders ────────────────────────────────────────────────────────────

def _place(eid: int, ts: float, family: str, category: str) -> ActionRecord:
    return ActionRecord(
        action_type="Place", element_id=eid, timestamp_unix=ts,
        element_category=category, family_name=family, type_name=f"{family} Type",
        view_id=301, operation_class="Model", transaction_name="Place",
    )


def _set(eid: int, ts: float, pname: str, family: str, category: str) -> ActionRecord:
    return ActionRecord(
        action_type="SetParam", element_id=eid, timestamp_unix=ts,
        element_category=category, family_name=family,
        param_name=pname, param_value_after=f"{pname}-val",
        view_id=301, operation_class="Parameter", transaction_name="Modify Parameter",
    )


def _tag(tag_eid: int, ts: float, tagged_eid: int, tag_family: str, tag_category: str) -> ActionRecord:
    return ActionRecord(
        action_type="Tag", element_id=tag_eid, timestamp_unix=ts,
        element_category=tag_category, family_name=tag_family, tag_family_name=tag_family,
        tagged_element_id=tagged_eid, view_id=301,
        operation_class="Annotation", transaction_name="Tag Element",
    )


def instance(
    eid: int,
    t0: float,
    *,
    family: str,
    tag_family: str,
    category: str,
    params: tuple[str, ...],
    step: float = 2.0,
) -> list[ActionRecord]:
    """One Place → SetParam* → Tag routine instance starting at t0."""
    recs = [_place(eid, t0, family, category)]
    for i, p in enumerate(params, start=1):
        recs.append(_set(eid, t0 + step * i, p, family, category))
    tag_ts = t0 + step * (len(params) + 1)
    recs.append(_tag(eid + 100_000, tag_ts, eid, tag_family, f"{category[:-1]} Tags"))
    return recs


def door(eid: int, t0: float, params: tuple[str, ...] = DOOR_PARAMS) -> list[ActionRecord]:
    return instance(eid, t0, family=DOOR_FAMILY, tag_family=DOOR_TAG,
                    category="Doors", params=params)


def window(eid: int, t0: float, params: tuple[str, ...] = WINDOW_PARAMS) -> list[ActionRecord]:
    return instance(eid, t0, family=WINDOW_FAMILY, tag_family=WINDOW_TAG,
                    category="Windows", params=params)


# ── Scenario builders (one per required test) ──────────────────────────────────

GAP = 60.0  # seconds between instances within a session (< default idle_gap)


def scenario_two_routines_param_diff() -> list[ActionRecord]:
    """3 doors + 3 windows — must form TWO separate clusters."""
    recs: list[ActionRecord] = []
    t = 0.0
    for k in range(3):
        recs += door(1000 + k, t); t += GAP
    for k in range(3):
        recs += window(2000 + k, t); t += GAP
    return recs


def scenario_interleaved() -> list[ActionRecord]:
    """door, window, door, window, door — door cluster 3, window cluster 2."""
    recs: list[ActionRecord] = []
    t = 0.0
    plan = [door, window, door, window, door]
    for k, make in enumerate(plan):
        recs += make(1000 + k, t)
        t += GAP
    return recs


def scenario_param_count_variation() -> list[ActionRecord]:
    """Same door routine, 4-param / 4-param / 3-param — must cluster TOGETHER."""
    recs: list[ActionRecord] = []
    t = 0.0
    recs += door(1000, t, params=("Mark", "Fire Rating", "Width", "Height")); t += GAP
    recs += door(1001, t, params=("Mark", "Fire Rating", "Width", "Height")); t += GAP
    recs += door(1002, t, params=("Mark", "Width", "Height")); t += GAP
    return recs


def scenario_idle_gap_split(idle_gap_minutes: float = 5.0) -> list[ActionRecord]:
    """Two doors separated by > idle_gap — must land in separate sessions."""
    recs: list[ActionRecord] = []
    recs += door(1000, 0.0)
    big_gap = idle_gap_minutes * 60.0 + 100.0  # safely beyond the idle gap
    recs += door(1001, recs[-1].timestamp_unix + big_gap)
    return recs


def scenario_cooldown_batches() -> tuple[list[ActionRecord], list[ActionRecord]]:
    """
    Two detect() batches for the cooldown test:
      batch1 — 3 doors (surfaces the routine)
      batch2 — those 3 doors + 2 more, all within the cooldown window
    """
    batch1: list[ActionRecord] = []
    t = 0.0
    for k in range(3):
        batch1 += door(1000 + k, t); t += GAP

    batch2 = list(batch1)
    for k in range(2):
        batch2 += door(2000 + k, t); t += GAP
    return batch1, batch2


# ── CLI: dump a sample session as JSONL ────────────────────────────────────────

if __name__ == "__main__":
    for rec in sorted(scenario_interleaved(), key=lambda r: r.timestamp_unix):
        print(json.dumps(rec.model_dump(exclude_none=True)))
