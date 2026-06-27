"""
v0.3 — compound (multi-element) routine detector.

v0.2 segments ONE element per instance (Place-rooted, SetParam/Tag attached by id), so a routine
that places several RELATED elements — the canonical "place a wall, then a door hosted on it, then
tag the door" — is split into separate single-element routines and the compound is lost. The
possibility matrix flags this exact case as `out_of_scope`.

v0.3 closes that gap deterministically: it assembles the same single-element instances (INCLUDING
short ones like a bare wall Place, which v0.2 discards), then MERGES a later element into the running
compound when its `Place.host_category` matches a category already in the compound AND it is
temporally adjacent (same session, within the idle gap). The merged compound instances are then run
through v0.2's own similarity clustering unchanged, so flat single-element routines still detect
exactly as before — v0.3 is a superset.

Over-merging risk is bounded by the host-link requirement: an element only joins a compound if it is
hosted on a category already present (an unhosted element, or one hosted on something not in the run,
starts a fresh instance). The hard tail (no host metadata) is left to optional future LLM segmentation.
"""
from __future__ import annotations

from shared.schemas import ActionRecord

from .v2_cluster import ClusterDetector, Instance


class CompoundDetector(ClusterDetector):
    """v0.3 detector — host-linked multi-element compounds, reusing v0.2 clustering."""

    name = "v3-compound"

    def _assemble_singles(self, records: list[ActionRecord]) -> list[Instance]:
        """v0.2-style segmentation WITHOUT the min-tokens filter, so short single-element instances
        (e.g. a bare wall Place) survive to be merged into a compound."""
        recs = sorted(records, key=lambda r: (r.timestamp_unix, r.element_id))
        idle_gap = self.config.idle_gap_minutes * 60.0
        open_instances: dict[int, Instance] = {}
        closed: list[Instance] = []
        session_index = 0
        last_time: float | None = None

        for r in recs:
            if last_time is not None and (r.timestamp_unix - last_time) > idle_gap:
                closed.extend(open_instances.values())
                open_instances.clear()
                session_index += 1
            last_time = r.timestamp_unix

            at = r.action_type
            if at == "Place":
                if r.element_id in open_instances:
                    closed.append(open_instances.pop(r.element_id))
                open_instances[r.element_id] = Instance(r.element_id, [r], session_index)
            elif at == "SetParam":
                inst = open_instances.get(r.element_id)
                if inst is not None:
                    inst.actions.append(r)
            elif at == "Tag":
                target = r.tagged_element_id if r.tagged_element_id is not None else r.element_id
                inst = open_instances.get(target)
                if inst is not None:
                    inst.actions.append(r)

        closed.extend(open_instances.values())
        singles = [i for i in closed if i.actions and i.actions[0].action_type == "Place"]
        singles.sort(key=lambda i: (i.start_time, i.element_id))
        return singles

    @staticmethod
    def _category(inst: Instance) -> str:
        p = inst.actions[0]
        return (p.element_category or p.family_name.split(":")[0]).strip()

    def segment(self, records: list[ActionRecord]) -> list[Instance]:
        idle_gap = self.config.idle_gap_minutes * 60.0
        singles = self._assemble_singles(records)

        merged: list[Instance] = []
        current: Instance | None = None
        cats: set[str] = set()
        for inst in singles:
            host = (inst.actions[0].host_category or "").strip()
            adjacent = current is not None and inst.session_index == current.session_index \
                and (inst.start_time - current.latest_time) <= idle_gap
            if adjacent and host and host in cats:
                current.actions.extend(inst.actions)        # hosted on something already in the compound
                cats.add(self._category(inst))
            else:
                if current is not None:
                    merged.append(current)
                current = Instance(inst.element_id, list(inst.actions), inst.session_index)
                cats = {self._category(inst)}
        if current is not None:
            merged.append(current)

        return [i for i in merged
                if len(i.actions) >= self.config.min_instance_tokens
                and i.actions[0].action_type == "Place"]
