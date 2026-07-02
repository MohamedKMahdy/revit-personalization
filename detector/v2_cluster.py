"""
v0.2 — similarity-clustering routine detector.

Pipeline (deterministic, standard-library only):

  1. Tokenize     — each record → "{action}:{key}" typed token.
  2. Segment      — open a new instance at each Place; append SetParam by
                    element_id and Tag by tagged_element_id; close on the next
                    Place for that element or on an idle gap. Idle gaps also
                    bump a session counter so gap-separated work splits into
                    separate sessions. Instances below min_instance_tokens are
                    discarded.
  3. Featurize    — per instance: ordered token sequence + a flat feature set
                    {fam:<family>, param:<name>…, tag:<family>}.
  4. Cluster      — greedy average-linkage grouping at threshold theta, where
                    similarity = w_set·Jaccard(featureset) + w_seq·(1 − normEdit(seq)).
  5. Threshold    — clusters with ≥ N members emit a CandidateRoutine whose
                    examples are the cluster members.
  6. Cooldown     — a signature surfaced within the last T minutes (by data
                    time) is suppressed; the existing cluster is grown instead.

Fixes the four v0.1 weaknesses: param/family routines no longer collapse
(tokens carry keys), interleaved repeats survive (segment by id, not position),
minor variation still groups (edit distance + Jaccard, not exact equality), and
only Place-rooted instances are mined (no arbitrary noisy substrings).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.schemas import ActionRecord, CandidateRoutine, RoutineExample

from .base import DetectorConfig
from ._common import (
    build_label,
    jaccard,
    normalized_edit_distance,
    routine_id_from_signature,
    short_signature,
    structural_signature,
    token,
)


@dataclass
class Instance:
    """One creation-rooted routine instance (the unit that gets clustered). Rooted at a Place
    (model element) or a Create (view / sheet / viewport — Track D: beyond instantiation)."""
    element_id:    int
    actions:       list[ActionRecord]
    session_index: int = 0

    @property
    def tokens(self) -> list[str]:
        return [token(a) for a in self.actions]

    @property
    def feature_set(self) -> frozenset[str]:
        feats: set[str] = set()
        for a in self.actions:
            if a.action_type == "Place":
                fam = a.family_name.split(":")[0].strip() or a.element_category
                feats.add(f"fam:{fam}")
            elif a.action_type == "Create":
                feats.add(f"create:{(a.type_name or '').strip() or a.element_category}")
            elif a.action_type == "SetParam":
                feats.add(f"param:{a.param_name or ''}")
            elif a.action_type == "Tag":
                feats.add(f"tag:{a.tag_family_name or ''}")
            elif a.action_type == "Modify":
                feats.add(f"mod:{a.element_category}")
        return frozenset(feats)

    @property
    def start_time(self) -> float:
        return min(a.timestamp_unix for a in self.actions)

    @property
    def latest_time(self) -> float:
        return max(a.timestamp_unix for a in self.actions)


class ClusterDetector:
    """v0.2 detector. Holds a cooldown ledger across detect() calls."""

    name = "v2-cluster"

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()
        # cooldown state: structural signature → last data-time it was surfaced
        self._surfaced: dict[str, float] = {}
        # latest (possibly grown) candidate per signature, for inspection
        self._store: dict[str, CandidateRoutine] = {}

    # ── 2. Segmentation ───────────────────────────────────────────────────────

    def segment(self, records: list[ActionRecord]) -> list[Instance]:
        recs = sorted(records, key=lambda r: (r.timestamp_unix, r.element_id))
        idle_gap = self.config.idle_gap_minutes * 60.0

        open_instances: dict[int, Instance] = {}
        closed: list[Instance] = []
        session_index = 0
        last_time: float | None = None

        for r in recs:
            if last_time is not None and (r.timestamp_unix - last_time) > idle_gap:
                # idle gap → close everything open, start a new session
                closed.extend(open_instances.values())
                open_instances.clear()
                session_index += 1
            last_time = r.timestamp_unix

            at = r.action_type
            if at in ("Place", "Create"):
                # roots: Place (model element) or Create (view/sheet/viewport — Track D). A duplicated
                # view + its renames/template SetParams is ONE instance keyed by the new view's id.
                if r.element_id in open_instances:
                    closed.append(open_instances.pop(r.element_id))
                open_instances[r.element_id] = Instance(r.element_id, [r], session_index)
            elif at in ("SetParam", "Modify"):
                inst = open_instances.get(r.element_id)
                if inst is not None:
                    inst.actions.append(r)
            elif at == "Tag":
                target = r.tagged_element_id if r.tagged_element_id is not None else r.element_id
                inst = open_instances.get(target)
                if inst is not None:
                    inst.actions.append(r)
            # Delete: ignored for instance assembly

        closed.extend(open_instances.values())

        instances = [
            i for i in closed
            if len(i.actions) >= self.config.min_instance_tokens
            and i.actions[0].action_type in ("Place", "Create")
        ]
        instances.sort(key=lambda i: (i.start_time, i.element_id))
        return instances

    # ── 3 & 4. Similarity + clustering ────────────────────────────────────────

    def similarity(self, a: Instance, b: Instance) -> float:
        set_sim = jaccard(a.feature_set, b.feature_set)
        seq_sim = 1.0 - normalized_edit_distance(a.tokens, b.tokens)
        return self.config.w_set * set_sim + self.config.w_seq * seq_sim

    def cluster(self, instances: list[Instance]) -> list[list[Instance]]:
        """
        Greedy average-linkage grouping at threshold theta.

        Deterministic tie-breaks: instances are consumed in segment() order
        (sorted by start_time then element_id); among clusters that tie on
        average similarity, the strict `>` keeps the earliest-created cluster
        (clusters are appended in creation order). So two runs on identical
        input always produce identical clusters in identical order.
        """
        clusters: list[list[Instance]] = []
        for inst in instances:
            best: list[Instance] | None = None
            best_sim = -1.0
            for members in clusters:
                avg = sum(self.similarity(inst, m) for m in members) / len(members)
                if avg >= self.config.theta and avg > best_sim:
                    best, best_sim = members, avg
            if best is None:
                clusters.append([inst])
            else:
                best.append(inst)
        return clusters

    def partition(self, records: list[ActionRecord]) -> dict[int, str]:
        """
        Instance-level grouping for clustering-quality scoring: maps each
        instance's Place element_id → its cluster key (including singletons).
        """
        clusters = self.cluster(self.segment(records))
        out: dict[int, str] = {}
        for idx, members in enumerate(clusters):
            for m in members:
                out[m.element_id] = f"cluster_{idx}"
        return out

    # ── cluster statistics ────────────────────────────────────────────────────

    def _mean_pairwise_similarity(self, members: list[Instance]) -> float:
        """Confidence (v0.2) = tightness = mean similarity over all member pairs."""
        n = len(members)
        if n < 2:
            return 1.0
        total = 0.0
        pairs = 0
        for i in range(n):
            for j in range(i + 1, n):
                total += self.similarity(members[i], members[j])
                pairs += 1
        return total / pairs if pairs else 1.0

    def _medoid(self, members: list[Instance]) -> Instance:
        """Most-representative member (max total similarity to the others)."""
        if len(members) == 1:
            return members[0]
        best = members[0]
        best_score = -1.0
        for m in members:
            score = sum(self.similarity(m, o) for o in members if o is not m)
            if score > best_score:
                best_score, best = score, m
        return best

    def _to_candidate(self, members: list[Instance], session_id: str) -> CandidateRoutine:
        medoid = self._medoid(members)
        sig = structural_signature(medoid.actions)
        ordered = sorted(members, key=lambda m: (m.start_time, m.element_id))
        examples = [
            RoutineExample(
                example_id=f"ex_{i + 1:03d}",
                session_id=session_id or f"session_{m.session_index}",
                recorded_at=m.start_time,
                actions=m.actions,
            )
            for i, m in enumerate(ordered)
        ]
        size = len(members)
        return CandidateRoutine(
            id=routine_id_from_signature(sig),
            label=build_label(medoid.actions),
            action_signature=short_signature(medoid.actions),
            count=size,
            support=size,
            confidence=round(self._mean_pairwise_similarity(members), 3),
            examples=examples,
        )

    # ── 5 & 6. Threshold + cooldown ───────────────────────────────────────────

    def detect(
        self,
        records: list[ActionRecord],
        *,
        session_id: str = "",
    ) -> list[CandidateRoutine]:
        """Run the full pipeline; return candidates newly surfaced this call."""
        clusters = self.cluster(self.segment(records))
        cooldown = self.config.cooldown_minutes * 60.0

        newly: list[CandidateRoutine] = []
        for members in clusters:
            if len(members) < self.config.min_cluster_size:
                continue

            sig = structural_signature(self._medoid(members).actions)
            candidate = self._to_candidate(members, session_id)
            self._store[sig] = candidate  # always reflect the latest (grown) cluster

            surfaced_at = max(m.latest_time for m in members)
            prev = self._surfaced.get(sig)
            if prev is not None and (surfaced_at - prev) < cooldown:
                # within cooldown → suppress; existing cluster has been grown above
                continue

            self._surfaced[sig] = surfaced_at
            newly.append(candidate)

        return newly

    # ── inspection helpers (used by tests / streaming UI) ─────────────────────

    def active_candidates(self) -> list[CandidateRoutine]:
        """All known candidates, including ones grown while in cooldown."""
        return sorted(self._store.values(), key=lambda r: r.support, reverse=True)

    def reset(self) -> None:
        """Clear cooldown + store (start a fresh detection session)."""
        self._surfaced.clear()
        self._store.clear()
