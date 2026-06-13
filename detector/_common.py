"""
Shared low-level helpers for both detector versions.

Kept dependency-free (standard library only) per the detection constraints:
no scikit-learn, no embedding models. Just key derivation, tokenization,
edit distance, Jaccard, and the label/signature formatters that keep routine
IDs compatible with the C# RoutineDetector and the MCP server lookup.
"""
from __future__ import annotations

from shared.schemas import ActionRecord

# ── Key derivation ────────────────────────────────────────────────────────────
# `key` is NOT a stored field in the v2.0 log schema, but it is fully derivable
# from fields the C# logger already emits — so no logger change is required.

def derive_key(rec: ActionRecord) -> str:
    """The discriminating key for a record, by action type.

      Place    → family_name (family portion only, before any ':')
      SetParam → param_name
      Tag      → tag_family_name
    """
    at = rec.action_type
    if at == "Place":
        return (rec.family_name.split(":")[0].strip() or rec.element_category)
    if at == "SetParam":
        return rec.param_name or ""
    if at == "Tag":
        return rec.tag_family_name or ""
    return ""


def token(rec: ActionRecord) -> str:
    """Typed token string: '{action_type}:{key}', e.g. 'SetParam:Mark'."""
    return f"{rec.action_type}:{derive_key(rec)}"


_ACTION_CHAR = {"Place": "P", "SetParam": "S", "Tag": "T", "Delete": "D"}


def action_char(rec: ActionRecord) -> str:
    """Single-character encoding used by the v0.1 substring baseline."""
    return _ACTION_CHAR.get(rec.action_type, "?")


# ── Similarity primitives ─────────────────────────────────────────────────────

def levenshtein(a: list, b: list) -> int:
    """Edit distance between two sequences of hashable tokens (iterative DP)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def normalized_edit_distance(a: list, b: list) -> float:
    """Levenshtein normalized to [0, 1] by the longer sequence length."""
    if not a and not b:
        return 0.0
    longest = max(len(a), len(b))
    return levenshtein(a, b) / longest if longest else 0.0


def jaccard(a: frozenset, b: frozenset) -> float:
    """Jaccard set similarity; two empty sets are defined as identical (1.0)."""
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


# ── Label / signature formatters (kept compatible with log_reader v1.5) ───────

def structural_signature(actions: list[ActionRecord]) -> str:
    """
    Full structural signature: '<category>|<family>|<action_sig>' where
    action_sig is e.g. 'Place,SetParam(Mark),SetParam(Width),Tag'.
    """
    parts = []
    for a in actions:
        if a.action_type == "Place":
            parts.append("Place")
        elif a.action_type == "SetParam":
            parts.append(f"SetParam({a.param_name or ''})")
        elif a.action_type == "Tag":
            parts.append("Tag")
        else:
            parts.append(a.action_type)
    category = actions[0].element_category if actions else ""
    family = actions[0].family_name.split(":")[0] if actions else ""
    return f"{category}|{family}|{','.join(parts)}"


def short_signature(actions: list[ActionRecord]) -> str:
    """Compact char signature for CandidateRoutine.action_signature: 'P,S,S,T'."""
    return ",".join((a.action_type[0] if a.action_type else "?") for a in actions)


def build_label(actions: list[ActionRecord]) -> str:
    """Human label: 'Place(M_Single-Flush) → SetParam×4 → Tag(Door Tag)'."""
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


def routine_id_from_signature(sig: str) -> str:
    """
    Routine ID generation — identical transform to log_reader / C# RoutineDetector
    so the orchestrator can still look routines up by ID.
    """
    transformed = (
        sig.replace("|", "_").replace(",", "_")
           .replace("(", "").replace(")", "").replace(" ", "")
    )
    return "routine_" + transformed[:40]
