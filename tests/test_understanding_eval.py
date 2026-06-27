"""
Regression test for the Stage 0 held-out understanding harness (eval/understanding_eval.py).

Locks the epistemic contract: supported conventions are understood (and beat literal replay), the
documented gaps stay unsolved (so over-generalization can't silently "pass"), and unpredictable
values are refused rather than fabricated.

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


def test_understanding_beats_detection_on_supported():
    by = _by_name()
    for n in ("door_mark_next", "window_mark_step5"):
        assert by[n]["und_ok"], f"{n} should be understood"
        assert not by[n]["det_ok"], f"{n} should defeat literal replay"
    # the stepped sequence specifically proves the STEP was learned, not a hardcoded +1
    assert by["window_mark_step5"]["understanding"] == "W-115"
    assert by["window_mark_step5"]["detection"] == "W-110"


def test_gaps_are_documented_not_silently_passed():
    by = _by_name()
    for n in ("mark_per_level_restart", "frame_by_width"):
        assert by[n]["kind"] == "gap"
        assert not by[n]["und_ok"], f"{n} is a known gap; passing it would be over-generalization"


def test_honesty_refuses_to_fabricate():
    by = _by_name()
    assert by["freetext_comment"]["understanding"] is None      # abstained
    assert by["freetext_comment"]["detection"] is not None      # literal replay would fabricate one


def test_summary_shows_the_gap():
    s = ue.summary(ue.run())
    assert s["understanding_transfer"][0] > s["detection_transfer"][0]   # understanding > detection
    assert s["detection_transfer"][0] == 0                              # literal replay transfers nothing
    assert s["supported_understood"] == (2, 2)
    assert s["honesty_refused"] == (1, 1)
    assert set(s["open_gaps"]) == {"mark_per_level_restart", "frame_by_width"}
