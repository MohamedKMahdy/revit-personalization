"""
Tests for the detection evaluation harness and the v1.5 detector adapter.

Run:  pytest tests/test_detection_eval.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from detector import EpisodeGroupingDetector, make_detector
from detector import synthetic as syn
from detector.v1_5_episode import _detect_routines_from_records
from eval import detection_eval as ev


# ── determinism ────────────────────────────────────────────────────────────────

def test_labeled_csv_is_deterministic():
    a = ev._rows_to_csv_string(ev.LABELED_FIELDS, ev.run_labeled())
    b = ev._rows_to_csv_string(ev.LABELED_FIELDS, ev.run_labeled())
    assert a == b


def test_sweep_csv_is_deterministic():
    a = ev._rows_to_csv_string(ev.SWEEP_FIELDS, ev.run_theta_sweep())
    b = ev._rows_to_csv_string(ev.SWEEP_FIELDS, ev.run_theta_sweep())
    assert a == b


# ── v1.5 behavior unchanged (adapter wraps the verbatim function) ──────────────

def test_v15_adapter_matches_verbatim_function():
    """EpisodeGroupingDetector.detect() must equal the verbatim function output
    (modulo the support field the adapter populates)."""
    records = syn.door(1000, 0.0) + syn.door(1001, 60.0) + syn.door(1002, 120.0)

    direct = _detect_routines_from_records(records, "t", min_repeats=3)
    viadet = EpisodeGroupingDetector().detect(records, session_id="t")

    assert len(direct) == len(viadet) == 1
    d, v = direct[0], viadet[0]
    assert (d.id, d.label, d.action_signature, d.count, d.confidence) == \
           (v.id, v.label, v.action_signature, v.count, v.confidence)
    # adapter-only addition:
    assert v.support == v.count == 3


def test_v15_splits_param_count_variation():
    """v1.5 uses exact param-name signatures, so a 3-vs-4 param variant of the
    same door routine splits into two signature groups (the weakness v0.2 fixes).
    With both groups below N=3, neither is emitted."""
    det = make_detector("v1.5")
    out = det.detect(syn.scenario_param_count_variation())  # 2x 4-param, 1x 3-param
    assert out == []


def test_v15_partition_is_consistent_with_detect():
    records, labels = syn.labeled_session()
    det = make_detector("v1.5")
    part = det.partition(records)
    # every labeled instance should be assigned a group
    for eid in labels:
        assert eid in part


# ── the headline comparison result ─────────────────────────────────────────────

def test_v2_beats_baselines_on_clustering_quality():
    rows = {r["detector"]: r for r in ev.run_labeled()}
    v2 = rows["v0.2-cluster"]
    v15 = rows["v1.5-episode"]
    v1 = rows["v0.1-substring"]

    # v0.2 perfectly recovers both routines and their groupings
    assert v2["routine_f1@0.80"] == 1.0
    assert v2["pairwise_f1"] == 1.0
    assert v2["ari"] == 1.0

    # v0.2 is at least as good as either baseline on every clustering metric,
    # and strictly better than at least one of them (the variant-splitting /
    # shape-merging weaknesses show up here).
    assert v2["ari"] >= v15["ari"]
    assert v2["ari"] >= v1["ari"]
    assert v2["ari"] > min(v15["ari"], v1["ari"])
