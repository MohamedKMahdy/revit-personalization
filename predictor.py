"""
Proactive next-action predictor (Pillar B — "realtime behavioural personalization").

Instead of waiting for 3 repeats then replaying, this predicts the NEXT action(s) while the user is
mid-routine: it matches the in-progress episode prefix (the element the user is currently working on,
read from the live eventlog) against the user's already-learned routines and offers to complete the
rest. Deterministic prefix-match first (~0 ms, no API) — the case that matters, since prediction is
about routines the user ALREADY repeats; an LLM fallback for genuinely novel prefixes is left as an
optional extension (kept out of the hot path so this is free + deterministic).

Reuses the detector tokenizer (detector._common.token) and the same routines the watcher detects, so
"what counts as the same step" is consistent across detection, execution, and prediction.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.schemas import ActionRecord, CandidateRoutine
from detector._common import token, action_char, build_label


@dataclass
class Prediction:
    routine_id: str
    routine_label: str
    support: int                       # how many times the user has repeated this routine
    confidence: float                  # 0-1: routine tightness, discounted for a loose (type-only) match
    match: str                         # "exact" (token prefix) | "type" (action-type prefix)
    next_actions: list[dict] = field(default_factory=list)   # remaining steps: {action_type, key}
    goal: str = ""                     # the routine's inferred intent.goal (the WHY), if known
    trigger: str = ""                  # the routine's inferred intent.trigger (the WHEN), if known

    @property
    def headline(self) -> str:
        """One-line human suggestion for the immediate next step (the inline chip text). When the
        routine's intent is known, it states the WHY ('to keep the door schedule complete') so the
        suggestion reflects understanding, not just sequence replay."""
        if not self.next_actions:
            return ""
        nxt = self.next_actions[0]
        verb = {"Place": "place", "SetParam": "set", "Tag": "tag with"}.get(nxt["action_type"], nxt["action_type"])
        what = nxt.get("key") or "it"
        more = f" (+{len(self.next_actions) - 1} more)" if len(self.next_actions) > 1 else ""
        # the goal is an INFERRED, unconfirmed hypothesis — hedge it ("looks like…?"), never assert it
        why = f" (looks like: {self.goal}?)" if self.goal else ""
        return f"You usually {verb} {what} next{more}{why} — apply your usual '{self.routine_label}'?"


def current_prefix(records: list[ActionRecord]) -> list[ActionRecord]:
    """The in-progress episode = the actions of the element the user is CURRENTLY working on: from the
    most-recent Place up to (not including) the next Place. Empty if nothing is in progress."""
    recs = sorted(records, key=lambda r: r.timestamp_unix)
    place_idxs = [i for i, r in enumerate(recs) if r.action_type == "Place"]
    if not place_idxs:
        return []
    start = place_idxs[-1]
    eid = recs[start].element_id
    prefix = [recs[start]]
    for r in recs[start + 1:]:
        if r.action_type == "Place":
            break
        if r.action_type == "SetParam" and r.element_id == eid:
            prefix.append(r)
        elif r.action_type == "Tag" and (r.tagged_element_id == eid or r.element_id == eid):
            prefix.append(r)
    return prefix


class NextActionPredictor:
    """Predict the next step(s) of an in-progress routine from the user's learned routines."""

    def __init__(self, routines: list[CandidateRoutine]):
        # canonical action sequence per routine = its most complete recorded example
        self._routines: list[tuple[CandidateRoutine, list[ActionRecord]]] = []
        for r in routines:
            canon = max((ex.actions for ex in r.examples), key=len, default=[])
            if canon:
                self._routines.append((r, canon))

    def predict(self, prefix: list[ActionRecord],
                intents: dict | None = None) -> Prediction | None:
        """Best next-step prediction for an in-progress prefix, or None if nothing matches.
        Prefers an EXACT typed-token prefix match (highest support wins); falls back to an
        action-TYPE prefix match (e.g. placed a door -> usually set a param + tag) at lower confidence.
        `intents` = {routine_id: motif.intent}: when the matched routine's intent is known, the
        prediction carries the WHY/WHEN so the suggestion reflects understanding, not just sequence."""
        if not prefix:
            return None
        ptok = [token(a) for a in prefix]
        ptype = [action_char(a) for a in prefix]
        n = len(prefix)

        exact: list[tuple[CandidateRoutine, list[ActionRecord]]] = []
        typed: list[tuple[CandidateRoutine, list[ActionRecord]]] = []
        for r, canon in self._routines:
            if len(canon) <= n:
                continue
            ctok = [token(a) for a in canon]
            if ctok[:n] == ptok:
                exact.append((r, canon))
            elif [action_char(a) for a in canon][:n] == ptype:
                typed.append((r, canon))

        if exact:
            r, canon = max(exact, key=lambda rc: (rc[0].support, rc[0].confidence))
            return self._to_prediction(r, canon, n, match="exact", conf=r.confidence or 1.0, intents=intents)
        if typed:
            r, canon = max(typed, key=lambda rc: (rc[0].support, rc[0].confidence))
            return self._to_prediction(r, canon, n, match="type", conf=0.5 * (r.confidence or 1.0), intents=intents)
        return None

    @staticmethod
    def _to_prediction(r: CandidateRoutine, canon: list[ActionRecord], n: int,
                       *, match: str, conf: float, intents: dict | None = None) -> Prediction:
        from detector._common import derive_key
        nxt = [{"action_type": a.action_type, "key": derive_key(a)} for a in canon[n:]]
        intent = (intents or {}).get(r.id) or {}
        return Prediction(routine_id=r.id, routine_label=r.label or build_label(canon),
                          support=r.support, confidence=round(conf, 3), match=match, next_actions=nxt,
                          goal=str(intent.get("goal") or ""), trigger=str(intent.get("trigger") or ""))


def predict_live(records: list[ActionRecord], routines: list[CandidateRoutine],
                 intents: dict | None = None) -> Prediction | None:
    """Convenience: predict the next action for the current in-progress episode in a live log."""
    return NextActionPredictor(routines).predict(current_prefix(records), intents=intents)
