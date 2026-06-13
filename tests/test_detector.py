"""
Detector test suite — the five required v0.2 scenarios plus contrast checks
against the v0.1 baseline.

Run:  pytest tests/test_detector.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from detector import ClusterDetector, DetectorConfig, SubstringDetector, make_detector
from detector import synthetic as syn


# ── helpers ────────────────────────────────────────────────────────────────────

def _families_in(members) -> set[str]:
    fams = set()
    for inst in members:
        for a in inst.actions:
            if a.action_type == "Place":
                fams.add(a.family_name)
    return fams


# ── Scenario 1: two routines differing by param/family must NOT merge ──────────

def test_param_diff_forms_two_clusters():
    det = ClusterDetector()
    records = syn.scenario_two_routines_param_diff()
    clusters = det.cluster(det.segment(records))

    sized = [c for c in clusters if len(c) >= 1]
    assert len(sized) == 2, f"expected 2 clusters, got {len(sized)}"

    sizes = sorted(len(c) for c in sized)
    assert sizes == [3, 3]

    fam_sets = [_families_in(c) for c in sized]
    assert {syn.DOOR_FAMILY} in fam_sets
    assert {syn.WINDOW_FAMILY} in fam_sets


# ── Scenario 2: interleaved repeats survive ────────────────────────────────────

def test_interleaved_yields_door3_window2():
    det = ClusterDetector()
    records = syn.scenario_interleaved()
    clusters = det.cluster(det.segment(records))

    by_family = {}
    for c in clusters:
        fams = _families_in(c)
        assert len(fams) == 1, "a cluster mixed families"
        by_family[next(iter(fams))] = len(c)

    assert by_family.get(syn.DOOR_FAMILY) == 3
    assert by_family.get(syn.WINDOW_FAMILY) == 2


# ── Scenario 3: 3-vs-4 param variation of the SAME routine clusters together ───

def test_param_count_variation_clusters_together():
    det = ClusterDetector()  # default theta = 0.80
    records = syn.scenario_param_count_variation()
    clusters = det.cluster(det.segment(records))

    big = [c for c in clusters if len(c) >= 2]
    assert len(big) == 1, f"variation split into {len(clusters)} clusters"
    assert len(big[0]) == 3

    # tightness < 1.0 because of the variation, but still a single cluster
    conf = det._mean_pairwise_similarity(big[0])
    assert 0.80 <= conf < 1.0


# ── Scenario 4: idle gap splits into separate sessions ─────────────────────────

def test_idle_gap_splits_sessions():
    cfg = DetectorConfig(idle_gap_minutes=5.0)
    det = ClusterDetector(cfg)
    records = syn.scenario_idle_gap_split(idle_gap_minutes=cfg.idle_gap_minutes)

    instances = det.segment(records)
    assert len(instances) == 2
    assert instances[0].session_index != instances[1].session_index


def test_no_gap_keeps_single_session():
    det = ClusterDetector(DetectorConfig(idle_gap_minutes=5.0))
    # two doors only 60s apart → same session
    records = syn.door(1000, 0.0) + syn.door(1001, 60.0)
    instances = det.segment(records)
    assert len(instances) == 2
    assert instances[0].session_index == instances[1].session_index


# ── Scenario 5: cooldown suppresses re-emission and grows the cluster ──────────

def test_cooldown_suppresses_and_grows():
    det = ClusterDetector(DetectorConfig(cooldown_minutes=10.0))
    batch1, batch2 = syn.scenario_cooldown_batches()

    first = det.detect(batch1)
    assert len(first) == 1, "first batch should surface the door routine"
    assert first[0].support == 3

    second = det.detect(batch2)
    assert second == [], "within cooldown the routine must not re-surface"

    # the existing cluster was grown to 5 members instead
    grown = det.active_candidates()
    assert len(grown) == 1
    assert grown[0].support == 5


def test_cooldown_expired_resurfaces():
    det = ClusterDetector(DetectorConfig(cooldown_minutes=10.0))
    batch1, batch2 = syn.scenario_cooldown_batches()

    det.detect(batch1)
    # shift batch2 well beyond the cooldown window
    for r in batch2:
        r.timestamp_unix += 100 * 60.0
    second = det.detect(batch2)
    assert len(second) == 1, "after cooldown expiry the routine should re-surface"
    assert second[0].support == 5


# ── Threshold + confidence semantics ───────────────────────────────────────────

def test_below_threshold_not_emitted():
    det = ClusterDetector(DetectorConfig(min_cluster_size=3))
    # only 2 windows → below N
    records = syn.window(2000, 0.0) + syn.window(2001, 60.0)
    assert det.detect(records) == []


def test_confidence_is_tightness_one_for_identical():
    det = ClusterDetector()
    records = syn.door(1000, 0.0) + syn.door(1001, 60.0) + syn.door(1002, 120.0)
    out = det.detect(records)
    assert len(out) == 1
    assert out[0].confidence == 1.0  # identical instances → perfectly tight
    assert out[0].support == 3


# ── v0.1 baseline contrast (documents why v0.2 is needed) ──────────────────────

def test_v1_merges_same_shape_different_routines():
    """
    v0.1 collapses to char-shape. A door (4 params) and a *different* 4-param
    routine share shape PSSSST and wrongly merge — the weakness v0.2 fixes.
    """
    det = SubstringDetector()
    # door with 4 params vs a 'sink' family with 4 different params: same shape.
    recs = []
    recs += syn.door(1000, 0.0)
    recs += syn.door(1001, 60.0)
    recs += syn.instance(3000, 120.0, family="M_Sink", tag_family="Plumbing Tag",
                         category="Plumbings", params=("A", "B", "C", "D"))
    out = det.detect(recs)
    # all three share shape "PSSSST" → one merged candidate (the bug)
    same_shape = [r for r in out if r.action_signature == "P,S,S,S,S,T"]
    assert len(same_shape) == 1
    assert same_shape[0].support == 3


def test_v1_splits_param_count_variation():
    """v0.1 exact-shape equality splits a 3-vs-4 param variant that v0.2 merges."""
    det = SubstringDetector()
    out = det.detect(syn.scenario_param_count_variation())
    # PSSSST (4-param, x2) is below N=3 and PSSST (3-param, x1) too → nothing emits,
    # i.e. the baseline fails to detect the routine the cluster detector finds.
    assert out == []


def test_factory_default_is_v2():
    assert make_detector().name == "v2-cluster"
    assert make_detector("v1").name == "v1-substring"
    assert make_detector("baseline").name == "v1-substring"
