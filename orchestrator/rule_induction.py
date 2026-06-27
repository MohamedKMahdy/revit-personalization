"""
Stage 1 rule induction (deterministic core).

Infers a generating RULE for a parameter's value from the user's examples *with their context*, so the
agent generalizes to held-out instances (the other side of a conditional, another instance on a known
level) instead of replaying the last value. Two families beyond a flat numeric sequence:

  conditional        -- the value is chosen by a condition on a context field (width>1500 -> "Wide").
                        Requires >=1 example on EACH branch: a branch never demonstrated cannot be
                        confirmed, so it is NOT invented (honest abstention, not a guess).
  per_context_seq    -- an independent sequence per a context field (Mark per level). Predicts the next
                        value within a context group it has SEEN; a brand-new group (an unseen level)
                        is under-determined from the data, so it abstains. Requires >=2 groups so the
                        keying is IDENTIFIABLE (L1-only data can't distinguish per-level from global).

Every candidate is kept only if it reproduces EVERY example with zero error -- the same
evidence-bounding discipline as pattern_agent._validate_and_downgrade. Otherwise induce_rule returns
None and the caller falls back to the flat sequence / literal value. (A later layer may have an LLM
PROPOSE richer templates for open-ended value_expr; this deterministic refit is the validator that
keeps such proposals honest.)

Examples are dicts: {"value": <str>, "context": {<key>: <value>, ...}}.
All functions are pure + deterministic + dependency-free.
"""
from __future__ import annotations

from .executor_agent import induce_sequence_rule, next_from_rule


def _num(v):
    """A context value as a float if it is numeric, else None."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def induce_conditional(examples: list, key: str) -> dict | None:
    """value chosen by a condition on context[key]. Handles a numeric THRESHOLD (two values cleanly
    separated by a cut point) and a CATEGORICAL map (each category -> one value). Needs >=2 branches,
    each with evidence."""
    pairs = [(e["value"], (e.get("context") or {}).get(key)) for e in examples]
    pairs = [(v, c) for v, c in pairs if v is not None and c is not None]
    if len(pairs) < 2:
        return None
    by_value: dict = {}
    for v, c in pairs:
        by_value.setdefault(v, []).append(c)

    # numeric threshold: exactly two values, separable by a cut point (max of low < min of high)
    if len(by_value) == 2 and all(all(_num(c) is not None for c in cs) for cs in by_value.values()):
        (va, ca), (vb, cb) = [(v, [_num(c) for c in cs]) for v, cs in by_value.items()]
        lo_v, lo, hi_v, hi = (va, ca, vb, cb) if max(ca) < max(cb) else (vb, cb, va, ca)
        if max(lo) < min(hi):                              # cleanly separated -> a threshold exists
            thr = (max(lo) + min(hi)) / 2.0
            return {"kind": "conditional", "key": key, "mode": "threshold",
                    "threshold": thr, "below": lo_v, "atleast": hi_v}

    # categorical: every category maps to exactly one value, with >=2 categories AND >=2 distinct
    # outputs (otherwise it's a constant, not a condition, and would just over-fit each seen category).
    by_ctx: dict = {}
    for v, c in pairs:
        by_ctx.setdefault(str(c), set()).add(v)
    if len(by_ctx) >= 2 and all(len(vs) == 1 for vs in by_ctx.values()):
        mapping = {c: next(iter(vs)) for c, vs in by_ctx.items()}
        if len(set(mapping.values())) >= 2:
            return {"kind": "conditional", "key": key, "mode": "category", "map": mapping}
    return None


def induce_per_context_seq(examples: list, key: str) -> dict | None:
    """An independent numeric sequence per context[key] (e.g. Mark per level). Requires >=2 context
    groups (so the keying is identifiable) and at least one group with an inducible sequence."""
    groups: dict = {}
    for e in examples:
        c = (e.get("context") or {}).get(key)
        if c is not None and e.get("value") is not None:
            groups.setdefault(str(c), []).append(e["value"])
    if len(groups) < 2:
        return None
    rules = {c: induce_sequence_rule(vs) for c, vs in groups.items()}
    rules = {c: r for c, r in rules.items() if r}
    if not rules:
        return None
    return {"kind": "per_context_seq", "key": key, "groups": rules}


def _reproduces(rule: dict, examples: list) -> bool:
    """A conditional rule must reproduce every example exactly (zero-error refit)."""
    if rule["kind"] != "conditional":
        return True                                       # per_context_seq is validated by construction
    for e in examples:
        c = (e.get("context") or {}).get(rule["key"])
        if c is None:
            return False
        if rule["mode"] == "threshold":
            n = _num(c)
            pred = rule["atleast"] if (n is not None and n >= rule["threshold"]) else rule["below"]
        else:
            pred = rule["map"].get(str(c))
        if pred != e["value"]:
            return False
    return True


def induce_rule(examples: list, context_keys: list | None = None) -> dict | None:
    """Try each rule family against the examples and return the first that REPRODUCES them all, else
    None. Conditionals are tried before context-keyed sequences."""
    if not examples:
        return None
    keys = context_keys or sorted({k for e in examples for k in (e.get("context") or {})})
    for key in keys:
        r = induce_conditional(examples, key)
        if r and _reproduces(r, examples):
            return r
    for key in keys:
        r = induce_per_context_seq(examples, key)
        if r:
            return r
    return None


def apply_rule(rule: dict, context: dict | None = None, used=None):
    """Predict the value for a held-out context. Returns None (honest abstention) when the rule cannot
    determine a value -- an unseen context group, or a context missing the rule's key."""
    context = context or {}
    if rule["kind"] == "conditional":
        c = context.get(rule["key"])
        if c is None:
            return None
        if rule["mode"] == "threshold":
            n = _num(c)
            return rule["atleast"] if (n is not None and n >= rule["threshold"]) else rule["below"]
        return rule["map"].get(str(c))                    # None if category never seen -> abstain
    if rule["kind"] == "per_context_seq":
        sub = rule["groups"].get(str(context.get(rule["key"])))
        return next_from_rule(sub, used) if sub else None  # unseen group -> abstain
    return None
