"""
Project memory — the assistant's persistent understanding of the user's Revit project.
The BIM analog of Claude Code's CLAUDE.md + memory files: a structured, human-readable
store that is loaded into the executor's context each run AND written back to as the
assistant learns, so understanding accumulates instead of being re-discovered every time.

The highest-value thing it remembers is the self-healing executor's CORRECTIONS:
  routine wanted M_Single-Flush → that family isn't loaded → executor used
  M_Door-Passage-Single-Flush. Remember that substitution and go STRAIGHT to the right
  family next time (no error, no re-discovery) — the system "knows the project".

Stored at %LOCALAPPDATA%\\RevitPersonalization\\project_memory.json (atomic writes).
Shape:
  {
    "project":     {"name_hint": "", "notes": [], "loaded_families": {category: [names]}},
    "routines":    { routine_id: { label, executions, family_substitutions:{wanted:used},
                                   last_host_wall_id, last_values:{param:value}, notes:[] } },
    "preferences": [ "free-text user preference", ... ]
  }
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MEM_PATH = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "RevitPersonalization" / "project_memory.json")


def _empty() -> dict:
    return {"project": {"name_hint": "", "notes": [], "loaded_families": {}},
            "routines": {}, "preferences": []}


def load() -> dict:
    try:
        mem = json.loads(MEM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    mem.setdefault("project", {}).setdefault("loaded_families", {})
    mem["project"].setdefault("name_hint", "")
    mem["project"].setdefault("notes", [])
    mem.setdefault("routines", {})
    mem.setdefault("preferences", [])
    return mem


def save(mem: dict) -> None:
    try:
        MEM_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEM_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(mem, indent=2), encoding="utf-8")
        tmp.replace(MEM_PATH)
    except Exception:
        pass


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


def add_preference(mem: dict, text: str) -> None:
    text = (text or "").strip()
    if text and text not in mem["preferences"]:
        mem["preferences"].append(text)


def remember_loaded_families(mem: dict, category: str, families: list[str]) -> None:
    if families:
        mem["project"]["loaded_families"][category] = sorted(set(families))


def to_prompt(mem: dict, routine_id: str) -> str:
    """Render what's known into a system-prompt block for the executor. Empty if nothing."""
    lines: list[str] = []
    r = mem["routines"].get(routine_id)
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
    if mem.get("preferences"):
        lines.append("User preferences: " + "; ".join(mem["preferences"]))
    if not lines:
        return ""
    return ("\n\nWHAT YOU ALREADY KNOW ABOUT THIS PROJECT (memory — apply it before guessing):\n- "
            + "\n- ".join(lines) + "\n")


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
