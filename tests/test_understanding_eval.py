"""
Regression test for the Stage 0/1 held-out understanding harness (eval/understanding_eval.py).

Locks the epistemic contract: supported conventions are understood (and beat literal replay), and
under-determined cases are refused rather than fabricated.

Run:  pytest tests/test_understanding_eval.py -v
"""
from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import understanding_eval as ue  # noqa: E402


def _by_name():
    return {r["name"]: r for r in ue.run()}


def test_all_supported_understood_and_beat_detection():
    by = _by_name()
    for r in by.values():
        if r["kind"] == "supported":
            assert r["und_ok"], f"{r['name']} should be understood"
            assert not r["det_ok"], f"{r['name']} should defeat literal replay"


def test_specific_generalizations():
    by = _by_name()
    assert by["window_mark_step5"]["understanding"] == "W-115"     # learned the step, not +1
    assert by["frame_by_width"]["understanding"] == "Wide"         # conditional -> held-out width
    assert by["mark_per_level"]["understanding"] == "D-204"        # per-level sequence, not global last


def test_honesty_probes_abstain():
    by = _by_name()
    for n in ("freetext_comment", "frame_one_branch_only", "mark_unseen_level"):
        assert by[n]["understanding"] is None, f"{n} must abstain (under-determined)"
        assert by[n]["detection"] is not None, f"{n}: literal replay would fabricate a value"


def test_summary_headline():
    s = ue.summary(ue.run())
    assert s["understanding_supported"] == (4, 4)
    assert s["detection_supported"] == (0, 4)        # literal replay generalizes to nothing
    assert s["understanding_honesty"] == (3, 3)      # abstains on all under-determined cases
    assert s["detection_honesty"] == (0, 3)
    assert s["failures"] == []
