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
             "last_host_wall_id": None, "last_values": {}, "corrections": [], "notes": []}
        mem["routines"][routine_id] = r
    if label and not r.get("label"):
        r["label"] = label
    r.setdefault("corrections", [])      # routines persisted before failure-learning existed
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
        # don't pollute memory with empty / "None" / "null" values (a stray one breaks the next-in-
        # sequence resolution — see executor_agent.resolve_routine_values).
        r["last_values"].update({k: str(v) for k, v in values.items()
                                 if v is not None and str(v).strip().lower() not in ("none", "null", "")})


def remember_loaded_families(mem: dict, category: str, families: list[str]) -> None:
    if families:
        mem["project"]["loaded_families"][category] = sorted(set(families))


# ── Cross-run failure learning ────────────────────────────────────────────────────
# The executor self-corrects WITHIN a run but starts blind every run, so it repeats the same
# opening mistakes (live logs: place_element returned "created 0" 7x across runs, none recovered).
# We mine the run's tool trace for CORRECTIONS — the failed approach + what to do instead — and
# store them per routine so to_prompt() can warn the model BEFORE its first action. Learning works
# from BOTH a failure→success delta AND a run that only ever failed (a caution is still high value;
# the unrecovered "created 0" is exactly the mistake the user sees repeated).
MAX_CORRECTIONS = 8

# Placement tools the executor can use. A door/window placed with bare place_element at a point
# with no host wall returns "created 0"; the robust path is place_and_configure WITH a host_wall_id.
_BARE_PLACE = {"place_element", "create_point_based_element",
               "create_line_based_element", "create_surface_based_element"}
_ATOMIC_PLACE = {"place_and_configure"}
_ALL_PLACE = _BARE_PLACE | _ATOMIC_PLACE
_SETPARAM = {"set_parameter", "set_element_parameter"}


def _ok(c: dict) -> bool:
    return bool((c.get("result") or {}).get("success"))


def _emsg(c: dict) -> str:
    return ((c.get("result") or {}).get("message") or "").strip()


def _norm_sig(msg: str) -> str:
    """A stable, human-readable signature for a failure message (for dedupe + display)."""
    return re.sub(r"\s+", " ", (msg or "").strip().rstrip(".")).lower()[:120] or "tool returned an error"


def _add_correction(mem: dict, routine_id: str, label: str, *, failed_tool: str, failed_signature: str,
                    fix: str, recovered: bool, run_date: str) -> None:
    """Upsert a correction, keyed on (failed_tool, failed_signature): bump `seen`, upgrade `fix`
    when a real recovery is found, and cap the list so stale corrections can't accumulate."""
    corr = routine_mem(mem, routine_id, label)["corrections"]
    for c in corr:
        if (c.get("failed_tool"), c.get("failed_signature")) == (failed_tool, failed_signature):
            c["seen"] = c.get("seen", 1) + 1
            if recovered or not c.get("recovered"):
                c["fix"] = fix                       # a confirmed fix supersedes an earlier caution
            c["recovered"] = bool(c.get("recovered") or recovered)
            if run_date:
                c["last_run"] = run_date
            break
    else:
        corr.append({"failed_tool": failed_tool, "failed_signature": failed_signature,
                     "fix": fix, "recovered": bool(recovered), "seen": 1, "last_run": run_date or ""})
    if len(corr) > MAX_CORRECTIONS:                  # keep the most-seen, then most-recent
        corr.sort(key=lambda c: (c.get("seen", 1), c.get("last_run", "")), reverse=True)
        del corr[MAX_CORRECTIONS:]


def learn_corrections(mem: dict, routine_id: str, label: str, tool_calls: list[dict], *,
                      nudged: int = 0, run_date: str | None = None) -> None:
    """Derive corrections from a run's tool trace so the executor stops repeating mistakes.
    Patterns (all reconstructable from the ordered tool_calls — retries stay in the same list):
      1. place_element failed → recovered with place_and_configure / a host_wall_id  (tool-switch fix)
      2. place_element failed and NEVER recovered                                    (caution + recovery)
      3. set_parameter failed → succeeded under a different name                     (wrong-name fix)
      4. the model stopped after placing and had to be nudged to finish             (ordering lesson)
    """
    if run_date is None:
        from datetime import date
        run_date = date.today().isoformat()
    calls = tool_calls or []

    # 1+2. placement — the dominant recurring mistake (wall-hosted family, bare point → "created 0").
    place_fail = [c for c in calls if c.get("name") in _BARE_PLACE and not _ok(c)]
    if place_fail:
        fam = (place_fail[0].get("args") or {}).get("family_name") \
            or (place_fail[0].get("args") or {}).get("name") or "this family"
        sig = _norm_sig(_emsg(place_fail[-1]) or "successfully created 0 element(s)")
        # A later successful placement WITH a host_wall_id (or a successful API placement) is the fix.
        host_ok = next((c for c in calls if c.get("name") in _ALL_PLACE and _ok(c)
                        and (c.get("args") or {}).get("host_wall_id")), None)
        api_ok = any(c.get("name") == "execute_revit_api" and _ok(c) for c in calls)
        recovered = bool(host_ok or api_ok)
        fix = (f"'{fam}' is wall-hosted — place_element returns 'created 0' if it has no host wall. "
               "FIRST get a host wall id (get_selected_elements for the user's selected wall, or "
               "pick_point ON a wall, or query the model for a wall), then call place_element with "
               "that host_wall_id and a point on the wall's centerline; then set parameters + tag.")
        if host_ok:
            fix += f" (host wall {(host_ok.get('args') or {}).get('host_wall_id')} worked here before)"
        elif not recovered:
            fix += " (never recovered last time — make sure host_wall_id is set before placing)"
        _add_correction(mem, routine_id, label, failed_tool="place_element", failed_signature=sig,
                        fix=fix, recovered=recovered, run_date=run_date)

    # 3. set_parameter — failed under one name, succeeded under another.
    sp_fail = [c for c in calls if c.get("name") in _SETPARAM and not _ok(c)]
    if sp_fail:
        bad = (sp_fail[0].get("args") or {}).get("name") or "the parameter"
        good = next((c for c in calls if c.get("name") in _SETPARAM and _ok(c)
                     and (c.get("args") or {}).get("name") and (c.get("args") or {}).get("name") != bad), None)
        if good:
            gn = (good.get("args") or {}).get("name")
            fix, recovered = f"the working parameter name is '{gn}', not '{bad}' — set '{gn}' directly", True
        else:
            fix, recovered = (f"setting parameter '{bad}' failed — verify it exists on this element "
                              "type before retrying"), False
        _add_correction(mem, routine_id, label, failed_tool="set_parameter",
                        failed_signature=_norm_sig(_emsg(sp_fail[-1]) or f"set_parameter {bad} failed"),
                        fix=fix, recovered=recovered, run_date=run_date)

    # 4. the model stopped after placing and had to be pushed to finish the routine.
    if nudged:
        _add_correction(mem, routine_id, label, failed_tool="(completion)",
                        failed_signature="stopped after placing without finishing the routine",
                        fix=("set EVERY parameter on the placed element and tag it in the SAME run as "
                             "the placement, before ending the turn — a placement alone is never done"),
                        recovered=True, run_date=run_date)


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


def corrections_block(mem: dict, routine_id: str) -> str:
    """The 'mistakes you made before on THIS routine' block — surfaced near the TOP of the executor
    prompt so the model applies the fix on its FIRST action instead of rediscovering it by failing."""
    r = mem.get("routines", {}).get(routine_id) or {}
    corr = r.get("corrections") or []
    if not corr:
        return ""
    top = sorted(corr, key=lambda c: (c.get("seen", 1), c.get("last_run", "")), reverse=True)[:3]
    bullets = []
    for c in top:
        last = c.get("last_run", "")
        tail = f" (seen {c.get('seen', 1)}x" + (f"; last {last}" if last else "") + ")"
        bullets.append(f"AVOID: {c.get('failed_signature', '')}. DO THIS INSTEAD: {c.get('fix', '')}{tail}.")
    return ("\n\nWHAT WENT WRONG BEFORE ON THIS ROUTINE — AVOID REPEATING IT (apply this on your FIRST "
            "action, before any place/set/tag):\n- " + "\n- ".join(bullets) + "\n")


def to_prompt(mem: dict, routine_id: str) -> str:
    """Full memory block for the executor: the user profile + mistakes-to-avoid + what's known."""
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
    block += corrections_block(mem, routine_id)        # mistakes-to-avoid, high in the prompt
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
