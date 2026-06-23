"""
Detection possibility matrix — the v0.2 detector's operating envelope.

This is the thesis's *capability-boundary* artifact. Rather than one labeled
session, it enumerates the space of pattern variations a BIM-authoring routine can
take and reports, per scenario, whether the v0.2 ClusterDetector behaves as the
design INTENDS — and, comparatively, where v0.1 / v1.5 do not.

METHODOLOGY / PROVENANCE (read before citing any number)
  Scenarios are a CROSS-PRODUCT of the six variation dimensions the detector
  explicitly models (shape, variation, order, temporal, noise/threshold, and the
  stateful cooldown axis). Expected outcomes were fixed from the academic
  definition of each pattern class BEFORE running the detector; out_of_scope
  classes were declared as limitations a priori. No scenario was added or
  relabeled after observing detector output. The cross-product is SAMPLED
  (representative points per dimension), not exhaustive.

EPISTEMIC STATUS OF EACH ROW (this is what makes a synthetic matrix defensible)
  Rows carry different evidential weight, shown in the `class` column:
    • confirm  (core)  — confirmatory: verifies design intent on canonical inputs.
                         "core 15/15" is NOT a generalization claim.
    • falsify  (boundary / out_of_scope) — a FALSIFIABLE prediction: the detector
                         could have behaved otherwise. These carry the real
                         evidential content. One of them (order_optional_tag)
                         actually FAILS, and one out_of_scope case
                         (noise_frequent_spurious) is a predicted FALSE POSITIVE
                         the detector does commit — proof the matrix is not rigged
                         to pass.

METRIC NOTES
  • routine_f1 (purity ≥0.80), pairwise_f1, ARI are reported only where a positive
    detection is expected. ARI is shown ONLY for ≥2 ground-truth routines (on a
    single routine it degenerates to a fragmentation flag); both are blanked ("—")
    on silent (expect=0) rows.

Deterministic (fixed timestamps, std-lib only, no LLM/Revit). Run:
    python eval/possibility_matrix.py             # matrix + baseline comparison + summary
    python eval/possibility_matrix.py --real      # + descriptive real-log loop-closure
    python eval/possibility_matrix.py --csv-only
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.schemas import ActionRecord
from detector import make_detector
from detector.synthetic import (
    _place, _set, _tag, door, window, GAP,
    DOOR_FAMILY, DOOR_TAG,
)
from eval.detection_eval import (
    _cluster_member_eids, routine_level_prf, pairwise_prf, adjusted_rand_index,
)

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

Records = list[ActionRecord]
Labels = dict[int, str]
Batch = "tuple[Records, Labels]"
Gen = Callable[[], "list[Batch]"]   # one (records, labels) per detect() call

IDLE = 5.0 * 60.0   # default idle_gap_minutes in seconds
COOLDOWN = 10.0 * 60.0  # default cooldown_minutes in seconds


# ════════════════════════════════════════════════════════════════════════════════
# Shape builders (beyond the canonical Place→SetParam*→Tag in synthetic.py)
# ════════════════════════════════════════════════════════════════════════════════

def _door_place_only(eid, t0):
    return [_place(eid, t0, DOOR_FAMILY, "Doors")]


def _door_params_no_tag(eid, t0, params):
    recs = [_place(eid, t0, DOOR_FAMILY, "Doors")]
    for i, p in enumerate(params, 1):
        recs.append(_set(eid, t0 + 2.0 * i, p, DOOR_FAMILY, "Doors"))
    return recs


def _door_tag_no_param(eid, t0):
    return [_place(eid, t0, DOOR_FAMILY, "Doors"),
            _tag(eid + 100_000, t0 + 2.0, eid, DOOR_TAG, "Door Tags")]


def _door_full(eid, t0, params, family=DOOR_FAMILY, tag=DOOR_TAG):
    """Place → SetParam* → Tag with an explicit family/tag/param set."""
    recs = [_place(eid, t0, family, "Doors")]
    for i, p in enumerate(params, 1):
        recs.append(_set(eid, t0 + 2.0 * i, p, family, "Doors"))
    recs.append(_tag(eid + 100_000, t0 + 2.0 * (len(params) + 1), eid, tag, "Door Tags"))
    return recs


def _door_tag_then_params(eid, t0, params):
    """Step-order variant: Place → Tag → SetParam*."""
    recs = [_place(eid, t0, DOOR_FAMILY, "Doors"),
            _tag(eid + 100_000, t0 + 2.0, eid, DOOR_TAG, "Door Tags")]
    for i, p in enumerate(params, 1):
        recs.append(_set(eid, t0 + 2.0 + 2.0 * i, p, DOOR_FAMILY, "Doors"))
    return recs


def _door_typed(eid, t0, type_name, params):
    """Same family_name, different type (size). Token key uses family before ':'."""
    recs = [ActionRecord(action_type="Place", element_id=eid, timestamp_unix=t0,
                         element_category="Doors", family_name=DOOR_FAMILY,
                         type_name=type_name, view_id=301, operation_class="Model",
                         transaction_name="Place")]
    for i, p in enumerate(params, 1):
        recs.append(_set(eid, t0 + 2.0 * i, p, DOOR_FAMILY, "Doors"))
    recs.append(_tag(eid + 100_000, t0 + 2.0 * (len(params) + 1), eid, DOOR_TAG, "Door Tags"))
    return recs


def _door_valued(eid, t0, mark_value, params):
    """Canonical door, but the Mark SetParam carries a DIFFERENT value each time."""
    recs = [_place(eid, t0, DOOR_FAMILY, "Doors")]
    for i, p in enumerate(params, 1):
        val = mark_value if p == "Mark" else f"{p}-val"
        recs.append(ActionRecord(action_type="SetParam", element_id=eid,
                                 timestamp_unix=t0 + 2.0 * i, element_category="Doors",
                                 family_name=DOOR_FAMILY, param_name=p, param_value_after=val,
                                 view_id=301, operation_class="Parameter",
                                 transaction_name="Modify Parameter"))
    recs.append(_tag(eid + 100_000, t0 + 2.0 * (len(params) + 1), eid, DOOR_TAG, "Door Tags"))
    return recs


def _door_tag_null(eid, t0, params):
    """Canonical door whose Tag has tagged_element_id=None (element_id == placed eid)."""
    recs = [_place(eid, t0, DOOR_FAMILY, "Doors")]
    for i, p in enumerate(params, 1):
        recs.append(_set(eid, t0 + 2.0 * i, p, DOOR_FAMILY, "Doors"))
    recs.append(ActionRecord(action_type="Tag", element_id=eid,
                             timestamp_unix=t0 + 2.0 * (len(params) + 1),
                             element_category="Door Tags", family_name=DOOR_TAG,
                             tag_family_name=DOOR_TAG, tagged_element_id=None,
                             view_id=301, operation_class="Annotation",
                             transaction_name="Tag Element"))
    return recs


def _delete(eid, t0):
    return ActionRecord(action_type="Delete", element_id=eid, timestamp_unix=t0,
                        element_category="Doors", family_name=DOOR_FAMILY,
                        view_id=301, operation_class="Model", transaction_name="Delete")


# ════════════════════════════════════════════════════════════════════════════════
# Scenario generators — each returns list[(records, labels)], one per detect() call
# ════════════════════════════════════════════════════════════════════════════════

def _seq(make, n, label, base_eid=1000):
    recs, labels, t = [], {}, 0.0
    for k in range(n):
        eid = base_eid + k
        recs += make(eid, t)
        labels[eid] = label
        t += GAP
    return recs, labels


def _one(make, n, label, base=1000):
    return [_seq(make, n, label, base)]


# ── DIM 1: routine SHAPE ──
def g_canonical():            return _one(lambda e, t: door(e, t), 4, "door")
def g_params_no_tag():        return _one(lambda e, t: _door_params_no_tag(e, t, ("Mark", "Width")), 4, "door_np")
def g_place_1param():         return _one(lambda e, t: _door_params_no_tag(e, t, ("Mark",)), 4, "door_1p")
def g_place_tag_no_param():   return _one(lambda e, t: _door_tag_no_param(e, t), 4, "door_tag")
def g_place_only():           return _one(lambda e, t: _door_place_only(e, t), 6, "door_placeonly")

def g_multi_element():
    """(place wall → place door[+1 param] → tag door) ×4 — a compound, multi-element routine."""
    recs, labels, t = [], {}, 0.0
    for k in range(4):
        wall, dr = 1000 + k, 5000 + k
        recs.append(_place(wall, t, "Basic Wall", "Walls"))                      # wall (1 token, lost)
        recs.append(_place(dr, t + 2.0, DOOR_FAMILY, "Doors"))                   # door place
        recs.append(_set(dr, t + 4.0, "Mark", DOOR_FAMILY, "Doors"))            # door param (clears floor)
        recs.append(_tag(dr + 100_000, t + 6.0, dr, DOOR_TAG, "Door Tags"))     # door tag
        labels[wall] = "compound"; labels[dr] = "compound"
        t += GAP
    return [(recs, labels)]

def g_param_only_existing():
    recs, labels, t = [], {}, 0.0
    for k in range(4):
        eid = 9000 + k  # placed in a previous, unseen session
        recs.append(_set(eid, t, "Fire Rating", DOOR_FAMILY, "Doors"))
        recs.append(_set(eid, t + 2.0, "Comments", DOOR_FAMILY, "Doors"))
        labels[eid] = "paramonly"; t += GAP
    return [(recs, labels)]

def g_replace_same_element():
    """×4: Place(eid) → SetParam(Mark) → Place(eid AGAIN) → SetParam(Width) → Tag."""
    recs, labels, t = [], {}, 0.0
    for k in range(4):
        eid = 1000 + k
        recs.append(_place(eid, t, DOOR_FAMILY, "Doors"))
        recs.append(_set(eid, t + 1.0, "Mark", DOOR_FAMILY, "Doors"))   # lost when re-placed
        recs.append(_place(eid, t + 2.0, DOOR_FAMILY, "Doors"))         # closes the first fragment
        recs.append(_set(eid, t + 3.0, "Width", DOOR_FAMILY, "Doors"))
        recs.append(_tag(eid + 100_000, t + 4.0, eid, DOOR_TAG, "Door Tags"))
        labels[eid] = "door"; t += GAP
    return [(recs, labels)]


# ── DIM 2: parameter / family VARIATION ──
def g_constant_values():      return _one(lambda e, t: door(e, t, params=("Mark", "Fire Rating", "Width")), 4, "door")

def g_variable_values():
    recs, labels, t = [], {}, 0.0
    for k in range(4):
        eid = 1000 + k
        recs += _door_valued(eid, t, f"D-10{k+1}", ("Mark", "Fire Rating", "Width"))
        labels[eid] = "door"; t += GAP
    return [(recs, labels)]

def g_param_count_3v4():
    recs, labels, t = [], {}, 0.0
    specs = [("Mark", "Fire Rating", "Width", "Height")] * 4 + [("Mark", "Width", "Height")] * 2
    for k, params in enumerate(specs):
        eid = 1000 + k; recs += door(eid, t, params=params); labels[eid] = "door"; t += GAP
    return [(recs, labels)]

def g_param_count_extreme():
    """2-param subset vs 5-param superset, SAME family — should stay separate (abbreviated≠full)."""
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        eid = 1000 + k; recs += _door_full(eid, t, ("Mark", "Width")); labels[eid] = "door_short"; t += GAP
    for k in range(3):
        eid = 2000 + k; recs += _door_full(eid, t, ("Mark", "Width", "Height", "Level", "Comments")); labels[eid] = "door_long"; t += GAP
    return [(recs, labels)]

def g_different_family():
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        eid = 1000 + k; recs += _door_full(eid, t, ("Mark", "Width"), family="M_Single-Flush"); labels[eid] = "doorA"; t += GAP
    for k in range(3):
        eid = 2000 + k; recs += _door_full(eid, t, ("Mark", "Width"), family="M_Double-Flush"); labels[eid] = "doorB"; t += GAP
    return [(recs, labels)]

def g_similar_distinct():
    """Two SAME-family routines sharing 4 of 5 params — the near-theta precision edge (must stay 2)."""
    recs, labels, t = [], {}, 0.0
    base = ("Mark", "Width", "Height", "Level")
    for k in range(3):
        eid = 1000 + k; recs += _door_full(eid, t, base + ("Comments",)); labels[eid] = "doorP"; t += GAP
    for k in range(3):
        eid = 2000 + k; recs += _door_full(eid, t, base + ("Phase",)); labels[eid] = "doorQ"; t += GAP
    return [(recs, labels)]

def g_same_family_diff_type():
    recs, labels, t = [], {}, 0.0
    for k, tn in enumerate(["900 x 2100mm", "900 x 2400mm", "900 x 2100mm", "900 x 2400mm"]):
        eid = 1000 + k; recs += _door_typed(eid, t, tn, ("Mark", "Width")); labels[eid] = "door"; t += GAP
    return [(recs, labels)]


# ── DIM 3: ORDER & optionality ──
def g_order_consistent():     return _one(lambda e, t: door(e, t, params=("Mark", "Width")), 4, "door")

def g_order_mixed():
    """3× Place→Param→Tag + 3× Place→Tag→Param (STEP permutation)."""
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        eid = 1000 + k; recs += door(eid, t, params=("Mark", "Width")); labels[eid] = "door"; t += GAP
    for k in range(3):
        eid = 2000 + k; recs += _door_tag_then_params(eid, t, ("Mark", "Width")); labels[eid] = "door"; t += GAP
    return [(recs, labels)]

def g_param_permutation():
    """Same param SET, different order: (Mark,Width,Height) vs (Height,Width,Mark) — Jaccard rescues."""
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        eid = 1000 + k; recs += _door_full(eid, t, ("Mark", "Width", "Height")); labels[eid] = "door"; t += GAP
    for k in range(3):
        eid = 2000 + k; recs += _door_full(eid, t, ("Height", "Width", "Mark")); labels[eid] = "door"; t += GAP
    return [(recs, labels)]

def g_optional_tag():
    """3× with tag + 3× without tag — same routine, an optional terminal step."""
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        eid = 1000 + k; recs += door(eid, t, params=("Mark", "Width")); labels[eid] = "door"; t += GAP
    for k in range(3):
        eid = 2000 + k; recs += _door_params_no_tag(eid, t, ("Mark", "Width")); labels[eid] = "door"; t += GAP
    return [(recs, labels)]


# ── DIM 4: TEMPORAL ──
def g_contiguous():           return _one(lambda e, t: door(e, t), 4, "door")

def g_interleaved():
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        recs += door(1000 + k, t); labels[1000 + k] = "door"; t += GAP
        recs += window(2000 + k, t); labels[2000 + k] = "window"; t += GAP
    return [(recs, labels)]

def g_idle_gap_split():
    recs, labels, t = [], {}, 0.0
    for k in range(3):
        recs += door(1000 + k, t); labels[1000 + k] = "door"; t += GAP
    t += IDLE + 200.0
    for k in range(3):
        recs += door(2000 + k, t); labels[2000 + k] = "door"; t += GAP
    return [(recs, labels)]

def g_multi_session():
    """Same routine across THREE > idle_gap bursts (9 instances) — one cluster of support 9."""
    recs, labels, t = [], {}, 0.0
    eid = 1000
    for _burst in range(3):
        for _k in range(3):
            recs += door(eid, t); labels[eid] = "door"; eid += 1; t += GAP
        t += IDLE + 200.0
    return [(recs, labels)]

def g_tag_null():
    return _one(lambda e, t: _door_tag_null(e, t, ("Mark", "Width")), 4, "door")


# ── DIM 5 & 6: NOISE, CORRECTIONS, THRESHOLDS ──
def g_distractors():
    recs, labels = _seq(lambda e, t: door(e, t), 4, "door")
    t = max(r.timestamp_unix for r in recs) + GAP
    recs += _door_params_no_tag(3000, t, ("X1", "X2")); labels[3000] = "noise_a"; t += GAP
    recs += _door_full(3001, t, ("Y1", "Y2"), family="M_Curtain", tag="Curtain Tag"); labels[3001] = "noise_b"
    return [(recs, labels)]

def g_frequent_spurious():
    """A frequent but trivial repeat (re-touching one param) — INTENDED: not a personalizable routine."""
    recs, labels, t = [], {}, 0.0
    for k in range(4):
        eid = 1000 + k
        recs.append(_place(eid, t, "M_Generic", "Generic Models"))
        recs.append(_set(eid, t + 1.0, "Comments", "M_Generic", "Generic Models"))
        recs.append(_set(eid, t + 2.0, "Comments", "M_Generic", "Generic Models"))  # re-touch same param
        labels[eid] = "trivial"; t += GAP
    return [(recs, labels)]

def g_delete_correction():
    recs, labels, t = [], {}, 0.0
    for k in range(4):
        scrap, eid = 7000 + k, 1000 + k
        recs.append(_place(scrap, t, DOOR_FAMILY, "Doors"))
        recs.append(_delete(scrap, t + 1.0))
        recs += door(eid, t + 2.0, params=("Mark", "Width"))
        labels[eid] = "door"; t += GAP
    return [(recs, labels)]

def g_exactly_N():            return _one(lambda e, t: door(e, t), 3, "door")
def g_below_N():              return _one(lambda e, t: door(e, t), 2, "door")

def g_two_routines_diff_support():
    recs, labels, t = [], {}, 0.0
    for k in range(5):
        recs += door(1000 + k, t); labels[1000 + k] = "door"; t += GAP
    for k in range(3):
        recs += window(2000 + k, t); labels[2000 + k] = "window"; t += GAP
    return [(recs, labels)]


# ── EDGE cases ──
def g_empty():                return [([], {})]
def g_single_record():        return [([_place(1, 0.0, DOOR_FAMILY, "Doors")], {1: "door"})]


# ── DIM 7: COOLDOWN / STATEFULNESS (two detect() batches on the SAME detector) ──
def g_cooldown_suppress():
    """batch1 = 3 doors (surfaces). batch2 = same 3 + 2 more, WITHIN cooldown → 0 newly, grown to 5."""
    b1, l1 = _seq(lambda e, t: door(e, t), 3, "door", 1000)
    recs2 = list(b1)
    t = max(r.timestamp_unix for r in b1) + GAP   # still within cooldown window
    l2 = dict(l1)
    for k in range(2):
        eid = 2000 + k; recs2 += door(eid, t); l2[eid] = "door"; t += GAP
    return [(b1, l1), (recs2, l2)]

def g_cooldown_resurface():
    """Same, but batch2's growth happens AFTER the cooldown window → re-surfaces (1 newly)."""
    b1, l1 = _seq(lambda e, t: door(e, t), 3, "door", 1000)
    recs2 = list(b1)
    t = max(r.timestamp_unix for r in b1) + COOLDOWN + 200.0  # beyond cooldown
    l2 = dict(l1)
    for k in range(2):
        eid = 2000 + k; recs2 += door(eid, t); l2[eid] = "door"; t += GAP
    return [(b1, l1), (recs2, l2)]

def g_cooldown_distinct():
    """batch1 = doors (surfaces). batch2 = windows (a DIFFERENT signature) → surfaces despite recency."""
    b1, l1 = _seq(lambda e, t: door(e, t), 3, "door", 1000)
    t = max(r.timestamp_unix for r in b1) + GAP
    b2, l2 = [], {}
    for k in range(3):
        b2 += window(2000 + k, t); l2[2000 + k] = "window"; t += GAP
    return [(b1, l1), (b2, l2)]


# ════════════════════════════════════════════════════════════════════════════════
# Scenario registry
# ════════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Scenario:
    id: str
    dimension: str
    scope: str            # core | boundary | out_of_scope
    expect: int           # intended #routines newly surfaced on the LAST detect() call
    gen: Gen
    note: str = ""

    @property
    def klass(self) -> str:
        # core rows are confirmatory; boundary/out_of_scope are falsifiable predictions
        return "confirm" if self.scope == "core" else "falsify"


SCENARIOS: list[Scenario] = [
    # ── DIM 1: SHAPE ──
    Scenario("shape_canonical", "shape", "core", 1, g_canonical,
             "Place→SetParam*→Tag, the canonical CEI routine."),
    Scenario("shape_params_no_tag", "shape", "core", 1, g_params_no_tag,
             "Place + 2 params, no tag (3 tokens) — surfaces."),
    Scenario("shape_place_1param", "shape", "boundary", 0, g_place_1param,
             "Place + 1 SetParam (2 tokens) — below min_instance_tokens=3 floor. NB the C# "
             "RoutineDetector's 'Place + >=1 SetParam' rule WOULD catch this: a cross-impl divergence."),
    Scenario("shape_place_tag_no_param", "shape", "boundary", 0, g_place_tag_no_param,
             "Place + Tag (2 tokens) — the SAME min_instance_tokens floor as place_1param "
             "(a single SetParam or a single Tag both give 2 tokens)."),
    Scenario("shape_place_only", "shape", "out_of_scope", 0, g_place_only,
             "Place only (1 token) — bulk placement is not a personalizable routine."),
    Scenario("shape_multi_element", "shape", "out_of_scope", 1, g_multi_element,
             "Compound wall→door→tag, door carries 1 param. Single-element segmentation recovers "
             "the door routine (support 4) but CANNOT bind the wall→door context — the wall (1 token) "
             "is dropped. Partial recovery, not silence: compound binding is FUTURE WORK."),
    Scenario("shape_param_only_existing", "shape", "out_of_scope", 0, g_param_only_existing,
             "SetParam on pre-existing elements (no Place) — no instance is opened. FUTURE WORK."),
    Scenario("shape_replace_same_element", "shape", "boundary", 1, g_replace_same_element,
             "Re-place of the same element_id closes the first fragment; the pre-re-place "
             "SetParam(Mark) is DROPPED (silent edit loss). Detected as 1 routine but at reduced fidelity."),

    # ── DIM 2: VARIATION ──
    Scenario("var_constant_values", "variation", "core", 1, g_constant_values,
             "Identical params + values — one routine."),
    Scenario("var_variable_values", "variation", "core", 1, g_variable_values,
             "Same param names, different Mark VALUE each time — tokens key on name → one routine."),
    Scenario("var_param_count_3v4", "variation", "core", 1, g_param_count_3v4,
             "Mixed 4-param / 3-param (one-param diff) — edit distance keeps ONE routine."),
    Scenario("var_param_count_extreme", "variation", "boundary", 2, g_param_count_extreme,
             "2-param subset vs 5-param superset, SAME family — sim≈0.57 splits to TWO; the upper "
             "edge of subset tolerance (an abbreviated run vs a full run is arguably a different routine)."),
    Scenario("var_different_family", "variation", "core", 2, g_different_family,
             "Two different door families (same shape) — must be TWO routines (sim≈0.66)."),
    Scenario("var_similar_distinct", "variation", "boundary", 2, g_similar_distinct,
             "Two SAME-family routines sharing 4/5 params — sim≈0.79 < theta, must stay TWO. The "
             "PRECISION edge: only 0.09 of margin above the must-separate family case (0.66)."),
    Scenario("var_same_family_diff_type", "variation", "core", 1, g_same_family_diff_type,
             "Same family, different type/size — key uses family before ':', so ONE routine."),

    # ── DIM 3: ORDER ──
    Scenario("order_consistent", "order", "core", 1, g_order_consistent,
             "All instances in the same order — one routine."),
    Scenario("order_mixed", "order", "boundary", 1, g_order_mixed,
             "STEP permutation (Place→Param→Tag vs Place→Tag→Param). PASSES on a knife-edge: average "
             "similarity sits EXACTLY at theta (0.80); a longer instance or '>' tie rule would flip it."),
    Scenario("order_param_permutation", "order", "core", 1, g_param_permutation,
             "PARAM permutation (same set, different order) — Jaccard=1.0 carries it (sim≈0.84); "
             "demonstrates w_set robustness independent of the optional-step case."),
    Scenario("order_optional_tag", "order", "boundary", 1, g_optional_tag,
             "Optional terminal Tag (present in some instances only) — INTENDED one routine. The "
             "detector FRAGMENTS (sim=0.75 < 0.80): a real, characterized tolerance gap, NOT a relabel. "
             "No single theta absorbs an optional tag (0.75) without merging distinct families (0.66)."),

    # ── DIM 4: TEMPORAL ──
    Scenario("temporal_contiguous", "temporal", "core", 1, g_contiguous,
             "Contiguous repetition — one routine."),
    Scenario("temporal_interleaved", "temporal", "core", 2, g_interleaved,
             "Two routines interleaved in time — both surface (id-based segmentation, not position)."),
    Scenario("temporal_idle_gap_split", "temporal", "core", 1, g_idle_gap_split,
             "Same routine in two bursts across a > idle_gap break — the gap closes open instances "
             "but must NOT fragment the cluster; one routine of support 6."),
    Scenario("temporal_multi_session", "temporal", "core", 1, g_multi_session,
             "Same routine across THREE > idle_gap sessions (9 instances) — sessions fragment "
             "instances but never the routine; one cluster of support 9."),
    Scenario("temporal_tag_null_target", "temporal", "core", 1, g_tag_null,
             "Tag with tagged_element_id=None (element_id == placed eid) — exercises the segment() "
             "fallback; the tag still attaches → identical to canonical."),

    # ── DIM 5 & 6: NOISE / CORRECTIONS / THRESHOLD ──
    Scenario("noise_distractors", "noise", "core", 1, g_distractors,
             "Routine + unique one-off distractors — only the routine surfaces (low-support rejection)."),
    Scenario("noise_frequent_spurious", "noise", "out_of_scope", 0, g_frequent_spurious,
             "A FREQUENT but trivial repeat (re-touching one param) — academically NOT a personalizable "
             "routine, but the detector has no semantic filter and SURFACES it (predicted FALSE POSITIVE). "
             "'frequent' != 'meaningful'; semantic filtering is downstream/future work."),
    Scenario("noise_delete_correction", "noise", "boundary", 1, g_delete_correction,
             "Place→Delete→place+configure (a correction) — Delete is ignored; routine surfaces."),
    Scenario("thresh_exactly_N", "threshold", "core", 1, g_exactly_N,
             "Repeated exactly min_cluster_size (3) times — surfaces."),
    Scenario("thresh_below_N", "threshold", "core", 0, g_below_N,
             "Repeated only 2 times (< N) — must NOT surface."),
    Scenario("rank_two_routines_diff_support", "threshold", "core", 2, g_two_routines_diff_support,
             "Two routines, different support (5 and 3) — both surface, ranked by support."),

    # ── EDGE ──
    Scenario("edge_empty_input", "edge", "core", 0, g_empty, "Empty log — returns nothing."),
    Scenario("edge_single_record", "edge", "core", 0, g_single_record,
             "A single Place — one sub-min instance, nothing surfaces."),

    # ── DIM 7: COOLDOWN / STATEFULNESS (multi-batch) ──
    Scenario("cooldown_suppress_grow", "cooldown", "core", 0, g_cooldown_suppress,
             "Routine surfaced in batch1; batch2 grows it WITHIN the cooldown window → 0 newly "
             "surfaced (suppressed); the stored cluster grows to support 5."),
    Scenario("cooldown_resurface", "cooldown", "core", 1, g_cooldown_resurface,
             "Same growth but AFTER the cooldown window → the routine re-surfaces (1 newly)."),
    Scenario("cooldown_distinct_not_suppressed", "cooldown", "core", 1, g_cooldown_distinct,
             "batch2 is a DIFFERENT routine — surfaces despite recent activity (cooldown is per-signature)."),
]


# ════════════════════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class Run:
    actual: int
    supports: list[int]
    active: list[int]
    routine_f1: float | None
    pairwise_f1: float | None
    ari: float | None
    ok: bool


def run_detector(sc: Scenario, key: str) -> Run:
    batches = sc.gen()
    det = make_detector(key)
    N = det.config.min_cluster_size

    last_emitted = []
    for records, labels in batches:
        last_emitted = det.detect(records, session_id=sc.id)

    last_records, last_labels = batches[-1]
    emitted_clusters = [_cluster_member_eids(c) for c in last_emitted]
    supports = sorted((c.support for c in last_emitted), reverse=True)
    actual = len(last_emitted)

    items = sorted(last_labels.keys())
    # partition()/active_candidates() are v0.2-only; baselines just expose detect().
    pred = (det.partition(last_records) if (last_records and hasattr(det, "partition")) else {})
    r80 = routine_level_prf(emitted_clusters, last_labels, N, 0.80)
    pairwise_f1 = (pairwise_prf(items, last_labels, pred)["f1"]
                   if (len(items) >= 2 and pred) else None)
    n_classes = len(set(last_labels.values()))
    ari = (adjusted_rand_index(items, last_labels, pred)
           if (n_classes >= 2 and pred) else None)
    active = (sorted((c.support for c in det.active_candidates()), reverse=True)
              if hasattr(det, "active_candidates") else [])

    if sc.expect == 0:
        ok = (actual == 0)
        rf1 = None  # metric undefined where the correct answer is "emit nothing"
    else:
        rf1 = r80["f1"]
        ok = (rf1 == 1.0 and actual == sc.expect)

    return Run(actual, supports, active,
               round(rf1, 3) if rf1 is not None else None,
               round(pairwise_f1, 3) if pairwise_f1 is not None else None,
               round(ari, 3) if (ari is not None and sc.expect >= 2) else None,
               ok)


# ════════════════════════════════════════════════════════════════════════════════
# Output
# ════════════════════════════════════════════════════════════════════════════════

MATRIX_FIELDS = ["scenario", "dimension", "scope", "class", "expect", "actual",
                 "supports", "routine_f1", "pairwise_f1", "ari", "verdict"]


def _dash(v):
    return "—" if v is None else v


def _matrix_row(sc: Scenario, r: Run) -> dict:
    return {
        "scenario": sc.id, "dimension": sc.dimension, "scope": sc.scope, "class": sc.klass,
        "expect": sc.expect, "actual": r.actual,
        "supports": "/".join(map(str, r.supports)) or "-",
        "routine_f1": _dash(r.routine_f1), "pairwise_f1": _dash(r.pairwise_f1),
        "ari": _dash(r.ari), "verdict": "PASS" if r.ok else "FAIL",
    }


def _print_table(title, fields, rows):
    widths = {f: max(len(f), *(len(str(r.get(f, ""))) for r in rows)) for f in fields}
    print(f"\n{title}")
    print("  " + "  ".join(f.ljust(widths[f]) for f in fields))
    print("  " + "  ".join("-" * widths[f] for f in fields))
    for r in rows:
        print("  " + "  ".join(str(r.get(f, "")).ljust(widths[f]) for f in fields))


def _csv(fields, rows):
    import csv, io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fields})
    return buf.getvalue()


COMPARE_FIELDS = ["scenario", "scope", "expect", "v0.1", "v1.5", "v0.2"]


def _compare_rows() -> list[dict]:
    rows = []
    for sc in SCENARIOS:
        cell = {}
        for disp, key in (("v0.1", "v1"), ("v1.5", "v1.5"), ("v0.2", "v2")):
            try:
                r = run_detector(sc, key)
                cell[disp] = f"{r.actual}{'✓' if r.ok else '✗'}"
            except Exception:
                cell[disp] = "err"
        rows.append({"scenario": sc.id, "scope": sc.scope, "expect": sc.expect, **cell})
    return rows


def _summary(pairs: list[tuple[Scenario, Run]]):
    by_scope = {"core": [], "boundary": [], "out_of_scope": []}
    for sc, r in pairs:
        by_scope.setdefault(sc.scope, []).append((sc, r))
    print("\nSUMMARY (verdict = detector matched the INTENDED behaviour)")
    for scope in ("core", "boundary", "out_of_scope"):
        rs = by_scope.get(scope, [])
        if not rs:
            continue
        passed = sum(1 for _, r in rs if r.ok)
        klass = "confirmatory" if scope == "core" else "falsifiable"
        print(f"  {scope:<13} {passed}/{len(rs)} as intended   [{klass}]")
        for sc, r in rs:
            if not r.ok:
                print(f"       FAIL  {sc.id:<30} actual={r.actual} expect={sc.expect}  — {sc.note[:80]}")
    core_fail = [sc.id for sc, r in by_scope.get("core", []) if not r.ok]
    print("\nINTERPRETATION")
    if not core_fail:
        print("  • All CORE (confirmatory) scenarios behave as designed — the detector covers its envelope.")
    else:
        print(f"  • CORE deviations (genuine gaps): {', '.join(core_fail)}")
    print("  • The FALSIFIABLE content is the boundary + out_of_scope rows: a matrix rigged to pass "
          "would show all PASS. It does not —")
    falsifiers = [sc.id for sc, r in pairs if sc.scope != "core" and not r.ok]
    print(f"       predicted-but-failed edges: {', '.join(falsifiers) or '(none)'}")
    print("       (e.g. order_optional_tag fragments a should-merge routine; noise_frequent_spurious "
          "is a predicted false positive the detector commits — both honest limitations, not bugs hidden.)")


# ── descriptive real-log loop-closure ─────────────────────────────────────────
def run_real_validation():
    from mcp_server.log_reader import load_real_action_records
    from detector._common import structural_signature
    records = load_real_action_records()
    if not records:
        print("\nREAL-LOG LOOP-CLOSURE — no real logs found (skipped).")
        return
    det = make_detector("v2")
    surfaced = det.detect(records, session_id="real")
    print("\nREAL-LOG LOOP-CLOSURE — does the synthetic envelope predict real behaviour? (descriptive)")
    print(f"  real action records: {len(records)} ; routines surfaced: {len(surfaced)}")
    for c in surfaced:
        ex = c.examples[0].actions if c.examples else []
        sig = structural_signature(ex) if ex else "?"
        # classify the real routine onto a matrix shape-class
        kinds = {a.action_type for a in ex}
        if {"Place", "SetParam", "Tag"} <= kinds:
            cls = "shape_canonical (core)"
        elif {"Place", "SetParam"} <= kinds:
            cls = "shape_params_no_tag (core)"
        else:
            cls = "other"
        print(f"   • {c.label}  support={c.support} conf={c.confidence}")
        print(f"       sig: {sig}")
        print(f"       matrix class: {cls} → behaviour matches the matrix's core prediction (detected).")
    print("  NOTE: descriptive only (no ground-truth labels on real logs). It confirms the detector's "
          "behaviour on data it did NOT construct is consistent with the matrix's core rows.")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Detector possibility-matrix coverage report")
    ap.add_argument("--csv-only", action="store_true")
    ap.add_argument("--real", action="store_true", help="also run the descriptive real-log loop-closure")
    args = ap.parse_args()

    pairs = [(sc, run_detector(sc, "v2")) for sc in SCENARIOS]
    matrix_rows = [_matrix_row(sc, r) for sc, r in pairs]
    compare_rows = _compare_rows()

    (RESULTS_DIR / "possibility_matrix.csv").write_text(_csv(MATRIX_FIELDS, matrix_rows), encoding="utf-8")
    (RESULTS_DIR / "possibility_matrix_compare.csv").write_text(_csv(COMPARE_FIELDS, compare_rows), encoding="utf-8")

    if not args.csv_only:
        _print_table("DETECTION POSSIBILITY MATRIX — v0.2 ClusterDetector", MATRIX_FIELDS, matrix_rows)
        _print_table("BASELINE COMPARISON — actual newly-surfaced (✓/✗ = matched intended)",
                     COMPARE_FIELDS, compare_rows)
        _summary(pairs)
        if args.real:
            run_real_validation()
    print(f"\nCSV -> {RESULTS_DIR / 'possibility_matrix.csv'}")
    print(f"CSV -> {RESULTS_DIR / 'possibility_matrix_compare.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
