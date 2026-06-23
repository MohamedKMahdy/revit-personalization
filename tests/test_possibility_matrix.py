"""
Regression guard for the detection possibility matrix (eval/possibility_matrix.py).

Locks the v0.2 detector's operating envelope: every CORE scenario must keep passing,
the two documented falsifiable limitations must keep FAILING (so the matrix stays
honest, not silently "fixed" into a green wall), the stateful cooldown axis must
behave, and v0.2 must keep dominating the baselines on the discriminating scenarios.

Run:  pytest tests/test_possibility_matrix.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import eval.possibility_matrix as pm  # noqa: E402


def _by_id(scenario_id: str):
    return next(s for s in pm.SCENARIOS if s.id == scenario_id)


def test_all_core_scenarios_pass():
    """Confirmatory rows: the detector must do what its design says on canonical inputs."""
    for sc in pm.SCENARIOS:
        if sc.scope == "core":
            r = pm.run_detector(sc, "v2")
            assert r.ok, f"CORE scenario {sc.id} regressed (actual={r.actual}, expect={sc.expect})"


def test_documented_limitations_still_fail():
    """The falsifiable failures must remain failures — they are honest, characterized limits."""
    fails = {sc.id for sc in pm.SCENARIOS if not pm.run_detector(sc, "v2").ok}
    assert fails == {"order_optional_tag", "noise_frequent_spurious"}, (
        f"the set of v0.2 failures changed: {fails}. If a limitation was fixed, update the "
        f"matrix's scope/expect and this test deliberately.")


def test_cooldown_axis_behaves():
    actual = {s.id: pm.run_detector(s, "v2").actual
              for s in pm.SCENARIOS if s.dimension == "cooldown"}
    assert actual["cooldown_suppress_grow"] == 0          # within window -> suppressed
    assert actual["cooldown_resurface"] == 1              # beyond window -> re-surfaces
    assert actual["cooldown_distinct_not_suppressed"] == 1  # different signature -> surfaces
    # the suppressed routine still GREW in the store
    grow = pm.run_detector(_by_id("cooldown_suppress_grow"), "v2")
    assert grow.active == [5]


def test_v2_dominates_baselines_on_discriminators():
    # v0.1 (substring) collapses two distinct families into one; v0.2 separates them.
    fam = _by_id("var_different_family")
    assert pm.run_detector(fam, "v1").ok is False
    assert pm.run_detector(fam, "v2").ok is True

    # v1.5 (episode) fragments a param permutation; v0.2 keeps it one routine.
    perm = _by_id("order_param_permutation")
    assert pm.run_detector(perm, "v1.5").ok is False
    assert pm.run_detector(perm, "v2").ok is True

    # v1.5 surfaces a place-only bulk placement; v0.2 stays silent.
    placeonly = _by_id("shape_place_only")
    assert pm.run_detector(placeonly, "v1.5").ok is False
    assert pm.run_detector(placeonly, "v2").ok is True


def test_deterministic():
    a = [pm._matrix_row(sc, pm.run_detector(sc, "v2")) for sc in pm.SCENARIOS]
    b = [pm._matrix_row(sc, pm.run_detector(sc, "v2")) for sc in pm.SCENARIOS]
    assert a == b


def test_scope_counts():
    """The envelope shape itself is part of the contract."""
    scopes = [s.scope for s in pm.SCENARIOS]
    assert scopes.count("core") == 23
    assert scopes.count("boundary") == 8
    assert scopes.count("out_of_scope") == 4
