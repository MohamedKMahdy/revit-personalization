"""
v1.5 — episode-grouping detector (the historical pre-v0.2 logic).

This is the detector that lived inline in `mcp_server/log_reader.py` before v0.2.
The grouping functions below are copied **verbatim** from that module (same
algorithm, same signatures, same routine-id transform, same frequency-based
confidence) so that v1.5 in the evaluation is exactly the historical behavior —
NOT a reimplementation or an "improved" version.

`EpisodeGroupingDetector` is a thin adapter that exposes that verbatim logic
behind the shared `Detector` protocol. The only thing the adapter adds is
populating `support` (= count) on the returned candidates, which the historical
function predates; it does not touch the grouping.
"""
from __future__ import annotations

from collections import defaultdict

from shared.schemas import ActionRecord, CandidateRoutine, RoutineExample

from .base import DetectorConfig


# ─────────────────────────────────────────────────────────────────────────────
# VERBATIM from mcp_server/log_reader.py (do not modify — historical baseline)
# ─────────────────────────────────────────────────────────────────────────────

def _episode_signature(actions: list[ActionRecord]) -> str:
    """
    Structural signature for a routine episode.

    Format: "<category>|<family>|<action_sig>"
    where action_sig is e.g. "Place,SetParam(Mark),SetParam(Fire Rating),Tag"

    Two episodes with the same signature are treated as instances of the same
    routine — consistent with Jang et al. (2023) lexicon-based grouping.
    """
    parts = []
    for a in actions:
        if a.action_type == "Place":
            parts.append("Place")
        elif a.action_type == "SetParam":
            parts.append(f"SetParam({a.param_name or ''})")
        elif a.action_type == "Tag":
            parts.append("Tag")

    category = actions[0].element_category if actions else ""
    family   = actions[0].family_name.split(":")[0] if actions else ""
    return f"{category}|{family}|{','.join(parts)}"


def _short_signature(actions: list[ActionRecord]) -> str:
    """Compact signature for CandidateRoutine.action_signature: e.g. 'P,S,S,T'"""
    return ",".join(
        (a.action_type[0] if a.action_type else "?") for a in actions
    )


def _build_label(actions: list[ActionRecord]) -> str:
    """Human-readable label: 'Place(Door-Passage-Single) → SetParam×1 → Tag'"""
    parts: list[str] = []
    set_count = 0
    for a in actions:
        if a.action_type == "Place":
            fname = a.family_name.split(":")[0] if a.family_name else a.element_category
            parts.append(f"Place({fname})")
        elif a.action_type == "SetParam":
            set_count += 1
        elif a.action_type == "Tag":
            if set_count:
                parts.append(f"SetParam×{set_count}")
                set_count = 0
            parts.append(f"Tag({a.tag_family_name or ''})")
    if set_count:
        parts.append(f"SetParam×{set_count}")
    return " → ".join(parts)


def _detect_routines_from_records(
    records: list[ActionRecord],
    session_id: str,
    min_repeats: int = 2,
) -> list[CandidateRoutine]:
    """
    Group action records into per-element episodes, then find repeated signatures.

    An episode for element E is: all records with element_id == E (or
    tagged_element_id == E for Tag records), sorted by timestamp_unix.

    Only episodes that start with a Place action are considered.
    """
    # Map element_id → list of ActionRecords involving that element
    element_actions: dict[int, list[ActionRecord]] = defaultdict(list)
    for r in sorted(records, key=lambda x: x.timestamp_unix):
        if r.action_type in ("Place", "SetParam"):
            element_actions[r.element_id].append(r)
        elif r.action_type == "Tag" and r.tagged_element_id is not None:
            # Attach tag to the element it labels
            element_actions[r.tagged_element_id].append(r)

    # Build episodes (only elements we witnessed being placed)
    episodes: list[tuple[int, list[ActionRecord]]] = [
        (eid, acts)
        for eid, acts in element_actions.items()
        if any(a.action_type == "Place" for a in acts)
    ]

    # Group by structural signature
    sig_to_episodes: dict[str, list[tuple[int, list[ActionRecord]]]] = defaultdict(list)
    for eid, actions in episodes:
        sig = _episode_signature(actions)
        sig_to_episodes[sig].append((eid, actions))

    # Convert groups with enough repeats into CandidateRoutine objects
    routines: list[CandidateRoutine] = []
    for _sig, group in sig_to_episodes.items():
        if len(group) < min_repeats:
            continue

        examples = [
            RoutineExample(
                example_id=f"ex_{i+1:03d}",
                session_id=session_id,
                recorded_at=actions[0].timestamp_unix,
                actions=actions,
            )
            for i, (_, actions) in enumerate(group)
        ]

        first_actions = group[0][1]
        confidence    = round(min(1.0, len(group) / 5), 2)
        routine_id    = (
            "routine_"
            + _sig.replace("|", "_").replace(",", "_").replace("(", "").replace(")", "").replace(" ", "")[:40]
        )

        routines.append(CandidateRoutine(
            id=routine_id,
            label=_build_label(first_actions),
            action_signature=_short_signature(first_actions),
            count=len(group),
            confidence=confidence,
            examples=examples,
        ))

    return routines

# ─────────────────────────────────────────────────────────────────────────────
# End verbatim block
# ─────────────────────────────────────────────────────────────────────────────


class EpisodeGroupingDetector:
    """
    v1.5 baseline — adapts the verbatim episode-grouping logic above to the
    shared Detector protocol. Strong baseline: param-aware structural signatures
    already distinguish routines by parameter/family (unlike the v0.1 substring
    matcher), but exact-signature equality still splits 3-vs-4 parameter variants
    of the same routine.
    """

    name = "v1.5-episode"

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()

    def detect(
        self,
        records: list[ActionRecord],
        *,
        session_id: str = "",
    ) -> list[CandidateRoutine]:
        # Verbatim historical grouping; min_repeats = configured min_cluster_size.
        routines = _detect_routines_from_records(
            records, session_id, min_repeats=self.config.min_cluster_size
        )
        # Adapter-only: populate the frequency axis the historical model predates.
        for r in routines:
            r.support = r.count
        return routines

    def partition(self, records: list[ActionRecord]) -> dict[int, str]:
        """
        Instance-level grouping for clustering-quality scoring: element_id →
        structural-signature group, including singletons. Built from the same
        episode construction + `_episode_signature` the detector uses, so it is
        consistent with detect() (no separate grouping logic).
        """
        element_actions: dict[int, list[ActionRecord]] = defaultdict(list)
        for r in sorted(records, key=lambda x: x.timestamp_unix):
            if r.action_type in ("Place", "SetParam"):
                element_actions[r.element_id].append(r)
            elif r.action_type == "Tag" and r.tagged_element_id is not None:
                element_actions[r.tagged_element_id].append(r)

        out: dict[int, str] = {}
        for eid, acts in element_actions.items():
            if any(a.action_type == "Place" for a in acts):
                out[eid] = _episode_signature(acts)
        return out
