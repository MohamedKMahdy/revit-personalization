"""
v0.1 — naive substring baseline (kept ONLY for the precision/recall comparison).

This is the detector v0.2 replaces. It is intentionally faithful to the
described baseline and therefore intentionally weak:

  • Every action is collapsed to a single character (P / S / T / D).
  • Episodes are contiguous slices delimited by each Place token.
  • Identical episodes are grouped by EXACT char-shape equality.

Consequent (documented) weaknesses, all exercised by the test suite:
  1. Param/family blind — all SetParams collapse to "S", so two routines with
     the same shape but different params/families merge into one candidate.
  2. Contiguity required — truly interleaved repeats are not recovered.
  3. Exact-equality — a 3-param vs 4-param variant of the SAME routine produces
     two different shapes (PSSST vs PSSSST) and is split apart.
  4. Shape-only — anything sharing a char-shape is grouped regardless of meaning.

NOTE on `confidence`: this baseline reports the original FREQUENCY-based
confidence (min(1, count/5)). That is a different axis from v0.2's tightness
confidence — do not compare the two numbers directly.
"""
from __future__ import annotations

from collections import defaultdict

from shared.schemas import ActionRecord, CandidateRoutine, RoutineExample

from .base import DetectorConfig
from ._common import action_char, build_label, structural_signature


class SubstringDetector:
    """v0.1 baseline detector."""

    name = "v1-substring"

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()

    def detect(
        self,
        records: list[ActionRecord],
        *,
        session_id: str = "",
    ) -> list[CandidateRoutine]:
        recs = sorted(records, key=lambda r: (r.timestamp_unix, r.element_id))

        # Contiguous episodes, delimited by each Place.
        episodes: list[list[ActionRecord]] = []
        current: list[ActionRecord] = []
        for r in recs:
            if r.action_type == "Place":
                if current:
                    episodes.append(current)
                current = [r]
            elif current:
                current.append(r)
        if current:
            episodes.append(current)

        # Group by EXACT char-shape (the crux of the baseline's weaknesses).
        by_shape: dict[str, list[list[ActionRecord]]] = defaultdict(list)
        for ep in episodes:
            if len(ep) < self.config.min_instance_tokens:
                continue
            shape = "".join(action_char(r) for r in ep)
            by_shape[shape].append(ep)

        routines: list[CandidateRoutine] = []
        for shape, eps in by_shape.items():
            if len(eps) < self.config.min_cluster_size:
                continue
            first = eps[0]
            examples = [
                RoutineExample(
                    example_id=f"ex_{i + 1:03d}",
                    session_id=session_id,
                    recorded_at=ep[0].timestamp_unix,
                    actions=ep,
                )
                for i, ep in enumerate(eps)
            ]
            size = len(eps)
            routines.append(CandidateRoutine(
                # ID keyed on shape — reflects that the baseline groups by shape,
                # not by structural signature.
                id="routine_v1_" + shape,
                label=build_label(first),
                action_signature=",".join(shape),
                count=size,
                support=size,
                confidence=round(min(1.0, size / 5), 2),  # FREQUENCY axis (v0.1)
                examples=examples,
            ))
        return routines
