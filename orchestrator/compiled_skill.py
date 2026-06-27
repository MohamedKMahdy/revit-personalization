"""
Compiled skills — deterministic replay of a learned routine (programming-by-demonstration).

The thesis claim is "pattern -> executable automation", but until now replay flattened the motif to
English and let the LLM re-derive the automation on EVERY run (non-deterministic, re-billed, identical
to the free-form copilot path). This closes that gap WITHOUT a rewrite:

  1. The first time a routine is confirmed, the existing agentic executor runs ONCE and succeeds.
  2. synthesize() distills that successful run's tool_calls into a small PARAMETERIZED JSON program —
     an ordered list of real_dispatch calls with named HOLES ({location}, {host_wall}, a per-step
     {Mark}=resolved value, and {e0}/{e1}=ids created earlier in the same program).
  3. On later runs, run_compiled() executes that program DETERMINISTICALLY via the same dispatch_fn
     (no LLM). The agentic executor remains the fallback when a step fails or a hole can't be bound.

This is distillation of ONE grounded demonstration, NOT program synthesis from scratch / a DSL /
search — it only ever captures tool calls the agent already made successfully. Honest novelty:
motif-guided distillation into verified deterministic replay, with a self-healing agent as escalation,
fully local + dependency-free.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

# Routine-constituent write tools (the steps a compiled skill captures); reads/picks are NOT compiled.
_PLACE = {"place_element", "place_and_configure", "create_point_based_element",
          "create_line_based_element", "create_surface_based_element", "duplicate_element"}
_SETPARAM = {"set_parameter", "set_element_parameter"}
_TAG = {"tag_element", "tag_walls", "tag_rooms"}
_ACTION_TOOLS = _PLACE | _SETPARAM | _TAG

_LOCATION_KEYS = {"location", "locationPoint"}
_HOST_KEYS = {"host_wall_id", "hostWallId"}


def _placed_id(result: dict):
    if not isinstance(result, dict):
        return None
    for k in ("element_id", "elementId"):
        if result.get(k) is not None:
            try:
                return int(result[k])
            except (TypeError, ValueError):
                pass
    resp = result.get("response")
    if isinstance(resp, list) and resp:
        try:
            return int(resp[0])
        except (TypeError, ValueError):
            pass
    return None


def synthesize(tool_calls: list[dict], variable_params: set | list | None = None) -> dict | None:
    """Distill a SUCCESSFUL run's tool_calls into a compiled-skill program, or None if there's no
    placement to anchor it. Holes: {location}, {host_wall}, {<VariableParam>}, and {eN} for the id
    created by the N-th placement (so set/tag reference the right element at replay)."""
    var = {str(v) for v in (variable_params or set())}
    id_to_hole: dict[int, str] = {}          # created element id -> "{eN}"
    n_placed = 0
    steps: list[dict] = []

    for c in tool_calls:
        name = c.get("name")
        if name not in _ACTION_TOOLS or not (c.get("result") or {}).get("success"):
            continue
        args = copy.deepcopy(c.get("args") or {})
        new_args: dict = {}
        for k, v in args.items():
            if k in _LOCATION_KEYS:
                new_args[k] = "{location}"
            elif k in _HOST_KEYS:
                new_args[k] = "{host_wall}"
            elif isinstance(v, int) and v in id_to_hole:
                new_args[k] = id_to_hole[v]                       # reference an earlier-created element
            elif name in _SETPARAM and k in ("value",) and str(args.get("name")) in var:
                new_args[k] = "{" + str(args.get("name")) + "}"    # variable param -> hole
            else:
                new_args[k] = v                                   # literal (family, type, constant param)
        steps.append({"tool": name, "args": new_args})
        if name in _PLACE:
            eid = _placed_id(c.get("result") or {})
            if eid is not None:
                id_to_hole[eid] = "{e" + str(n_placed) + "}"
                n_placed += 1

    if not any(s["tool"] in _PLACE for s in steps):
        return None                                              # nothing to anchor a replay on
    return {"version": 1, "steps": steps}


def required_bindings(skill: dict) -> set:
    """The external holes a caller must supply to replay (excludes {eN}, which are bound at runtime)."""
    needed: set = set()
    for s in (skill.get("steps") or []):
        for v in (s.get("args") or {}).values():
            for hole in _holes_in(v):
                if not hole.startswith("e") or not hole[1:].isdigit():
                    needed.add(hole)
    return needed


def _holes_in(v: Any) -> list[str]:
    out: list[str] = []
    if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
        out.append(v[1:-1])
    elif isinstance(v, dict):
        for x in v.values():
            out.extend(_holes_in(x))
    return out


def can_replay(skill: dict, bindings: dict) -> bool:
    """True iff every external hole the program needs is available in bindings (precondition check)."""
    return required_bindings(skill).issubset({k for k, v in (bindings or {}).items() if v not in (None, "")})


def _fill(v: Any, bindings: dict, placed: list) -> Any:
    if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
        hole = v[1:-1]
        if hole.startswith("e") and hole[1:].isdigit():
            idx = int(hole[1:])
            return placed[idx] if idx < len(placed) else v
        return bindings.get(hole, v)
    if isinstance(v, dict):
        return {k: _fill(x, bindings, placed) for k, x in v.items()}
    return v


def run_compiled(skill: dict, bindings: dict, dispatch_fn: Callable[[str, dict], dict],
                 on_event: Callable[[str, Any], None] | None = None) -> dict:
    """Replay a compiled skill DETERMINISTICALLY via dispatch_fn (no LLM). Returns
    {done, compiled:True, tool_calls, failed_step}. On the first failed step, stops so the caller can
    fall back to the agentic executor."""
    emit = on_event or (lambda *_: None)
    placed: list = []
    tool_calls: list[dict] = []
    for i, step in enumerate(skill.get("steps") or []):
        tool = step.get("tool")
        args = {k: _fill(v, bindings, placed) for k, v in (step.get("args") or {}).items()}
        # any unresolved hole -> can't replay this step deterministically -> bail to the agent
        if any(_holes_in(v) for v in args.values()):
            emit("reasoning", f"Compiled replay can't bind step {i+1} ({tool}) — handing off to the agent.")
            return {"done": False, "compiled": True, "tool_calls": tool_calls, "failed_step": i}
        emit("tool", {"name": tool, "args": args})
        try:
            result = dispatch_fn(tool, args)
        except Exception as exc:
            result = {"success": False, "message": f"dispatch raised: {exc}"}
        emit("result", {"name": tool, "result": result})
        tool_calls.append({"name": tool, "args": args, "result": result})
        if not (result or {}).get("success"):
            return {"done": False, "compiled": True, "tool_calls": tool_calls, "failed_step": i}
        if tool in _PLACE:
            eid = _placed_id(result)
            if eid is not None:
                placed.append(eid)
    return {"done": True, "compiled": True, "tool_calls": tool_calls, "failed_step": None}
