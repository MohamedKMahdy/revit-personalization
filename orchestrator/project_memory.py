"""
Per-user persistent memory — the assistant's evolving understanding of each user and their
Revit project. The BIM analog of Claude Code's CLAUDE.md + memory files, with the per-user,
self-evolving model OpenClaw popularised: a structured, human-readable store that is loaded
into the agent's context each session AND written back to as the assistant learns, so
understanding accumulates per user instead of being re-discovered every time.

Why per-user (the thesis is about PERSONALIZATION): memory is scoped to a user id so two people
on the same machine — or many participants in the study — each get their own evolving profile,
preferences, and learned routine corrections. Identity resolution (best-effort, local):
    REVIT_USER_ID env override  →  the OS account name  →  "default".

Design choices vs OpenClaw/Mem0 (honest, committee-defensible): we keep memory FILE-BASED and
loaded into context (OpenClaw's USER.md/MEMORY.md model) rather than a vector-RAG layer (Mem0 /
Letta). At single-user-per-install scale, context-loading the high-signal facts is simpler, fully
inspectable, and dependency-free; the store stays swappable to a RAG backend if multi-tenant
scale ever demands it.

Stored at  %LOCALAPPDATA%\\RevitPersonalization\\users\\<user_id>\\memory.json  (atomic writes).
Shape:
  {
    "user":    {"id", "name_hint", "role_hint",
                "preferences": [free-text], "conventions": {name: value}, "notes": [free-text]},
    "project": {"name_hint", "notes": [], "loaded_families": {category: [names]}},
    "routines":{ routine_id: { label, executions, family_substitutions:{wanted:used},
                               last_host_wall_id, last_values:{param:value}, notes:[] } }
  }
"""
from __future__ import annotations

import getpass
import json
import os
import re
from pathlib import Path

_BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "RevitPersonalization"
MEM_ROOT = _BASE / "users"
LEGACY_PATH = _BASE / "project_memory.json"   # the old single global store (one-time import)


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", (name or "").strip()).strip("_.").lower()


def current_user() -> str:
    """Best-effort stable per-user id for this session."""
    for cand in (os.environ.get("REVIT_USER_ID"), _safe_getuser()):
        uid = _sanitize(cand or "")
        if uid:
            return uid
    return "default"


def _safe_getuser() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def user_path(user_id: str | None = None) -> Path:
    return MEM_ROOT / (_sanitize(user_id) if user_id else current_user()) / "memory.json"


# Default path for the CURRENT user (tests monkeypatch this for isolation).
MEM_PATH = user_path()


def _empty() -> dict:
    return {"user": {"id": "", "name_hint": "", "role_hint": "",
                     "preferences": [], "conventions": {}, "notes": []},
            "project": {"name_hint": "", "notes": [], "loaded_families": {}},
            "routines": {}}


def _coerce(mem: dict) -> dict:
    """Fill defaults + migrate the old top-level `preferences` into the user profile."""
    u = mem.setdefault("user", {})
    u.setdefault("id", "")
    u.setdefault("name_hint", "")
    u.setdefault("role_hint", "")
    u.setdefault("preferences", [])
    u.setdefault("conventions", {})
    u.setdefault("notes", [])
    if mem.get("preferences"):                       # migrate legacy global shape
        for p in mem.pop("preferences"):
            if p not in u["preferences"]:
                u["preferences"].append(p)
    p = mem.setdefault("project", {})
    p.setdefault("name_hint", "")
    p.setdefault("notes", [])
    p.setdefault("loaded_families", {})
    mem.setdefault("routines", {})
    return mem


def load(user_id: str | None = None) -> dict:
    """Load a user's memory. First run imports the old global store once (migration)."""
    path = user_path(user_id) if user_id else MEM_PATH
    if path.exists():
        try:
            return _coerce(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return _coerce(_empty())
    if LEGACY_PATH.exists():                          # one-time migration of the pre-per-user store
        try:
            return _coerce(json.loads(LEGACY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return _coerce(_empty())


def save(mem: dict, user_id: str | None = None) -> None:
    path = user_path(user_id) if user_id else MEM_PATH
    try:
        mem.setdefault("user", {})["id"] = _sanitize(user_id) if user_id else current_user()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


# ── User profile (the "remembers you" layer) ──────────────────────────────────────
def set_user(mem: dict, *, name: str | None = None, role: str | None = None) -> None:
    u = mem.setdefault("user", {})
    if name:
        u["name_hint"] = name.strip()
    if role:
        u["role_hint"] = role.strip()


def add_preference(mem: dict, text: str) -> None:
    text = (text or "").strip()
    prefs = mem.setdefault("user", {}).setdefault("preferences", [])
    if text and text not in prefs:
        prefs.append(text)


def add_convention(mem: dict, key: str, value: str) -> None:
    key = (key or "").strip()
    if key:
        mem.setdefault("user", {}).setdefault("conventions", {})[key] = str(value)


def note_about_user(mem: dict, text: str) -> None:
    text = (text or "").strip()
    notes = mem.setdefault("user", {}).setdefault("notes", [])
    if text and text not in notes:
        notes.append(text)


# ── Routine / project memory ──────────────────────────────────────────────────────
def routine_mem(mem: dict, routine_id: str, label: str = "") -> dict:
    r = mem["routines"].get(routine_id)
    if r is None:
        r = {"label": label, "executions": 0, "family_substitutions": {},
             "last_host_wall_id": None, "last_values": {}, "notes": []}
        mem["routines"][routine_id] = r
    if label and not r.get("label"):
        r["label"] = label
    return r


def learn_substitution(mem: dict, routine_id: str, wanted: str, used: str, label: str = "") -> None:
    if not wanted or not used or wanted == used:
        return
    routine_mem(mem, routine_id, label)["family_substitutions"][wanted] = used


def record_execution(mem: dict, routine_id: str, *, host_wall_id=None,
                     values: dict | None = None, label: str = "") -> None:
    r = routine_mem(mem, routine_id, label)
    r["executions"] = r.get("executions", 0) + 1
    if host_wall_id:
        r["last_host_wall_id"] = int(host_wall_id)
    if values:
        r["last_values"].update({k: str(v) for k, v in values.items()})


def remember_loaded_families(mem: dict, category: str, families: list[str]) -> None:
    if families:
        mem["project"]["loaded_families"][category] = sorted(set(families))


# ── Rendering into context ────────────────────────────────────────────────────────
def user_block(mem: dict) -> str:
    """The per-user profile rendered for a system prompt (used by the chat + executor)."""
    u = mem.get("user") or {}
    lines: list[str] = []
    who = []
    if u.get("name_hint"):
        who.append(u["name_hint"])
    if u.get("role_hint"):
        who.append(f"({u['role_hint']})")
    if who:
        lines.append("User: " + " ".join(who))
    if u.get("preferences"):
        lines.append("Preferences: " + "; ".join(u["preferences"]))
    if u.get("conventions"):
        lines.append("Conventions: " + ", ".join(f"{k} = {v}" for k, v in u["conventions"].items()))
    if u.get("notes"):
        lines.append("Notes: " + "; ".join(u["notes"]))
    if not lines:
        return ""
    return ("WHO YOU'RE WORKING WITH (persistent per-user memory — apply it, and respect the "
            "user's stated preferences):\n- " + "\n- ".join(lines) + "\n")


def to_prompt(mem: dict, routine_id: str) -> str:
    """Full memory block for the executor: the user profile + what's known about this routine."""
    lines: list[str] = []
    r = mem.get("routines", {}).get(routine_id)
    if r:
        subs = r.get("family_substitutions") or {}
        if subs:
            lines.append("Known family substitutions for THIS routine — use the mapped family "
                         "directly (it is what is loaded), do NOT retry the unmapped one: "
                         + "; ".join(f"'{k}' -> '{v}'" for k, v in subs.items()))
        if r.get("last_host_wall_id"):
            lines.append(f"This routine last hosted on wall id {r['last_host_wall_id']} — "
                         "reuse it as host_wall_id if it still fits.")
        if r.get("last_values"):
            lines.append("Last parameter values used: "
                         + ", ".join(f"{k}={v}" for k, v in r["last_values"].items())
                         + " (the user may want the next in sequence, e.g. Mark D-101 -> D-102).")
        if r.get("executions"):
            lines.append(f"You've completed this routine {r['executions']} time(s) here before.")
    loaded = (mem.get("project") or {}).get("loaded_families") or {}
    if loaded:
        parts = []
        for cat, fams in loaded.items():
            short = cat.replace("OST_", "")
            parts.append(f"{short}: " + ", ".join(fams[:8]) + (" …" if len(fams) > 8 else ""))
        lines.append("Families already known to be LOADED in this model (no need to re-query "
                     "get_available_family_types — place one of these): " + " | ".join(parts))

    block = user_block(mem)
    if lines:
        block += ("\n\nWHAT YOU ALREADY KNOW ABOUT THIS PROJECT (memory — apply it before guessing):\n- "
                  + "\n- ".join(lines) + "\n")
    return block


def learn_from_run(mem: dict, routine_id: str, label: str, tool_calls: list[dict], done: bool) -> None:
    """Write back what the executor learned this run: family substitution, host wall, values,
    and facts it discovered by QUERYING the live model (which families are loaded)."""
    # Facts learned from the model itself — cache so next run doesn't re-discover them.
    for c in tool_calls:
        if c.get("name") == "get_available_family_types" and (c.get("result") or {}).get("success"):
            cat = (c.get("args") or {}).get("category")
            fams = sorted({t.get("family") for t in (c["result"].get("types") or []) if t.get("family")})
            if cat and fams:
                remember_loaded_families(mem, cat, fams)

    places = [c for c in tool_calls if c.get("name") == "place_element"]
    wanted = places[0]["args"].get("family_name") if places else None
    used = next((c["args"].get("family_name") for c in places
                 if c["result"].get("success")), None)
    if wanted and used:
        learn_substitution(mem, routine_id, wanted, used, label)

    host = next((c["args"].get("host_wall_id") for c in places
                 if c["result"].get("success") and c["args"].get("host_wall_id")), None)
    values = {c["args"].get("name"): c["args"].get("value") for c in tool_calls
              if c.get("name") == "set_parameter" and c["result"].get("success")}
    if done:
        record_execution(mem, routine_id, host_wall_id=host, values=values, label=label)
