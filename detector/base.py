"""
Detector interface + configuration shared by the v0.1 baseline and v0.2.

Both detectors implement the same `Detector` protocol so they are
interchangeable in `list_candidate_routines()` and the evaluation harness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shared.schemas import ActionRecord, CandidateRoutine


@dataclass(frozen=True)
class DetectorConfig:
    """
    Tunable detection parameters. Defaults match the v0.2 design spec.

      min_cluster_size     N  — min members for a cluster to emit a candidate
      theta                   — similarity threshold for grouping (0–1)
      cooldown_minutes     T  — suppress re-emitting the same signature within T
      min_instance_tokens     — discard instances shorter than this many tokens
      idle_gap_minutes        — a gap larger than this closes the open instances
                                 and starts a new session
      w_set                   — weight on feature-set (Jaccard) similarity
      w_seq                   — weight on sequence (1 - norm edit dist) similarity
                                 (w_set + w_seq should sum to 1.0)
    """
    min_cluster_size:    int   = 3
    theta:               float = 0.80
    cooldown_minutes:    float = 10.0
    min_instance_tokens: int   = 3
    idle_gap_minutes:    float = 5.0
    w_set:               float = 0.6
    w_seq:               float = 0.4


@runtime_checkable
class Detector(Protocol):
    """
    A detection gate: read action records, return candidate routines.

    Deterministic and side-effect free with respect to the outside world
    (no Revit calls, no disk/model writes). Detectors MAY hold in-memory state
    across calls (e.g. the v0.2 cooldown ledger) — that is internal, not an
    external side effect.
    """
    name: str

    def detect(
        self,
        records: list[ActionRecord],
        *,
        session_id: str = "",
    ) -> list[CandidateRoutine]:
        ...
