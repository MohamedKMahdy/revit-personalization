"""
Stage 3/4 — turn the agent's inferred latents into CONFIRMABLE, self-improving understanding.

`describe_understanding()` renders the induced parameter rules + the motif's intent as plain-language
HYPOTHESES the user can confirm or correct (mixed-initiative interaction, Horvitz 1999). Confirmation
status lives in per-user memory (project_memory.confirm_understanding); a repeatedly-corrected rule is
auto-demoted (Stage 4) so the agent stops trusting it. Every inferred latent is also appended to an
understanding LEDGER (a thesis artifact: what was inferred, confirmed/corrected, and from how much
evidence) so "understanding" is auditable, not asserted.

Deterministic + $0 (reuses the Stage 1 inducers); no LLM, no Revit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .executor_agent import _example_contexts, induce_sequence_rule
from .rule_induction import induce_rule

LEDGER_PATH = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
               / "RevitPersonalization" / "logs" / "understanding_ledger.jsonl")


def _fmt_threshold(t) -> str:
    """Render the cut point so the text matches the EXECUTED rule (apply_rule classifies on the float):
    integer thresholds print plainly, fractional ones keep one decimal (e.g. 1300.5, not 1300)."""
    return str(int(t)) if float(t).is_integer() else str(round(float(t), 1))


def _describe_rule(rule: dict, pn: str) -> str:
    kind = rule.get("kind")
    if kind == "conditional":
        if rule["mode"] == "threshold":
            t = _fmt_threshold(rule["threshold"])
            return (f"You set {pn} by {rule['key']}: '{rule['below']}' when {rule['key']} is below "
                    f"{t}, '{rule['atleast']}' when it is at least {t}.")
        pairs = ", ".join(f"{c} → '{v}'" for c, v in rule["map"].items())
        return f"You set {pn} by {rule['key']}: {pairs}."
    if kind == "per_context_seq":
        groups = ", ".join(sorted(rule.get("groups") or {}))
        # scope the claim to the context groups we actually have evidence for (we abstain on new ones)
        return (f"You number {pn} per {rule['key']} — each restarts its own sequence "
                f"(learned for {rule['key']}s {groups}; I'll ask about any new ones).")
    return ""


def _rule_fingerprint(rule: dict | None) -> str:
    """A stable signature of the induced rule's STRUCTURE (not its rendered wording), so memory can tell
    whether a re-induced rule materially changed and therefore needs re-confirmation."""
    if not rule:
        return ""
    k = rule.get("kind")
    if k == "conditional":
        if rule["mode"] == "threshold":
            return f"cond:thr:{rule['key']}:{_fmt_threshold(rule['threshold'])}:{rule['below']}|{rule['atleast']}"
        return "cond:cat:{}:{}".format(rule["key"], ",".join(f"{c}={v}" for c, v in sorted(rule["map"].items())))
    if k == "per_context_seq":
        return "pcs:{}:{}".format(rule["key"], ",".join(sorted(rule.get("groups") or {})))
    return ""


def _seq_fingerprint(srule: dict | None) -> str:
    if not srule:
        return ""
    return f"seq:{srule['step']}:{srule['prefix']}|{srule['suffix']}:{srule['pad']}"


def _describe_sequence(rule: dict, pn: str) -> str:
    step = rule["step"]
    cur = f"{rule['prefix']}{str(rule['last']).zfill(rule['pad'])}{rule['suffix']}"
    nxt = f"{rule['prefix']}{str(rule['last'] + step).zfill(rule['pad'])}{rule['suffix']}"
    if step == 1:
        return f"You number {pn} sequentially (e.g. {cur} → {nxt})."
    return f"You number {pn} in steps of {step} (e.g. {cur} → {nxt})."


def describe_understanding(motif: dict, examples: list | None = None) -> list:
    """Plain-language hypotheses of what the agent understands about this routine — one per variable
    parameter with an inducible rule, plus the routine's intent (goal/trigger). Each is
    {key, statement, kind} where key is stable ('rule:<param>' / 'intent:goal' / 'intent:trigger')."""
    examples = examples if isinstance(examples, list) else []
    steps = motif.get("steps") if isinstance(motif, dict) else []
    out: list = []
    seen: set = set()
    for s in (steps or []):
        if not isinstance(s, dict):
            continue                                       # tolerate corrupted/hand-edited records
        pn = s.get("param_name")
        if not pn or pn in seen:
            continue
        is_variable = (s.get("param_value_type") or "").lower() == "variable" or s.get("param_value") in (None, "")
        if not is_variable:
            continue
        seen.add(pn)
        ctx_examples = _example_contexts(examples, pn)
        rule = induce_rule(ctx_examples)
        if rule:
            stmt, fp = _describe_rule(rule, pn), _rule_fingerprint(rule)
        else:
            srule = induce_sequence_rule([c["value"] for c in ctx_examples])
            stmt, fp = (_describe_sequence(srule, pn), _seq_fingerprint(srule)) if srule else ("", "")
        if stmt:
            out.append({"key": f"rule:{pn}", "statement": stmt, "kind": "rule", "fingerprint": fp})
    intent = (motif.get("intent") if isinstance(motif, dict) else None) or {}
    if intent.get("goal"):
        out.append({"key": "intent:goal", "statement": f"This routine's goal: {intent['goal']}.",
                    "kind": "intent", "fingerprint": f"goal:{intent['goal']}"})
    if intent.get("trigger"):
        out.append({"key": "intent:trigger", "statement": f"It should fire when {intent['trigger']}.",
                    "kind": "intent", "fingerprint": f"trigger:{intent['trigger']}"})
    return out


def log_understanding(routine_id: str, entries: list, path: str | None = None) -> None:
    """Append inferred-latent records to the understanding ledger (best-effort, never raises)."""
    p = Path(path) if path else LEDGER_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps({"routine_id": routine_id, **e}, ensure_ascii=False) + "\n")
    except Exception:
        pass
