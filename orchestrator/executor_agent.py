"""
Self-healing execution agent — runs a learned routine in the LIVE Revit model with
an agentic tool-use loop (the same pattern Claude Code uses): call a tool, feed the
RESULT — including errors — back to the model, and let it diagnose + retry.

Why this exists: the Macro Agent generates a fixed tool_sequence OFFLINE, blind to the
live model, so it cannot react to "no host wall" or "family not loaded". This executor
runs WITH live feedback, so it recovers from exactly those failures.

The loop (Anthropic tool-use):
    while not done and iters < cap:
        resp = claude.create(tools=TOOLS, messages=...)
        if resp wants tools:
            for each tool_use: result = dispatch(tool, args)   # actually run it in Revit
            append tool_results (is_error set on failure)       # <- the model sees the error
        else:
            done                                                # model returned a summary

Safety: only the allow-listed tools below are dispatchable (NEVER send_code_to_revit),
the loop is capped, and every step is streamed out via on_event so the user sees the
self-correction like a Claude Code transcript.

Everything is injectable (client, dispatch_fn, on_event) so the loop is unit-testable
without the Anthropic API or a live Revit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from orchestrator import revit_tools

EXECUTOR_MODEL = os.environ.get("EXECUTOR_MODEL", "claude-sonnet-4-6")
MAX_ITERS = int(os.environ.get("EXECUTOR_MAX_ITERS", "14"))

# ── The tools the executor may use (Anthropic schema). This list IS the allowlist. ──
# CURATED_SCHEMAS are the ergonomic, hand-tuned tools for the common routine path
# (place/set/tag + the model-grounding reads). The full plugin surface (create walls,
# floors, grids, levels, rooms, dimensions, color, queries, duplicate, delete, atomic
# groups, image export, …) is appended from revit_tools below, so the executor has the
# FULL capabilities of the backend. send_code_to_revit is never included.
CURATED_SCHEMAS: list[dict] = [
    {
        "name": "place_element",
        "description": (
            "Place a family instance at a point (millimetres). For a hosted family "
            "(door/window) it snaps to the nearest wall within ~1.5 m; if there is no wall "
            "you get a 'no valid host' error — recover by picking a point on a wall. If the "
            "family is not loaded you get a 'created 0' / 'type not found' error — recover by "
            "listing available types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "family_name": {"type": "string", "description": "Family name, e.g. 'M_Door-Passage-Single-Flush'"},
                "location": {
                    "type": "object",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
                    "required": ["x", "y"],
                },
                "type_name": {"type": "string", "description": "Optional type/size, e.g. '900 x 2100mm'"},
                "host_wall_id": {"type": "integer", "description": "Optional explicit host wall element id"},
            },
            "required": ["family_name", "location"],
        },
    },
    {
        "name": "set_parameter",
        "description": "Set a parameter on a placed element by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["element_id", "name", "value"],
        },
    },
    {
        "name": "tag_element",
        "description": "Tag a placed element in the active view (auto-selects a tag family by category).",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "integer"}},
            "required": ["element_id"],
        },
    },
    {
        "name": "get_available_family_types",
        "description": (
            "List the family types LOADED in the project for a category (e.g. 'OST_Doors', "
            "'OST_Windows', 'OST_Walls'). Use this to recover when a placement fails because "
            "the requested family is not loaded — then retry with the closest loaded family."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"category": {"type": "string"}},
            "required": ["category"],
        },
    },
    {
        "name": "get_active_view",
        "description": "Get the active Revit view (name, type, scale). Point placement needs a "
                       "plan or 3D view; use this to know the context before placing.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "inspect_model",
        "description": "Count elements by category in the model (Walls, Doors, Windows, Floors, "
                       "Levels, ...). Use this to CHECK whether the model has walls before placing a "
                       "wall-hosted door/window — if Walls is 0 there is nothing to host on, so tell "
                       "the user instead of failing.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_selected_elements",
        "description": "Get the elements the user currently has SELECTED in Revit. If they selected a "
                       "wall, use its id as host_wall_id to place the door on exactly that wall.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pick_point",
        "description": (
            "Ask the USER to click the placement point in Revit. Use this when you need a "
            "location, or to host a door/window on a specific wall (tell the user to click on "
            "a wall). Returns the clicked point in mm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": [],
        },
    },
]

# ── Revit API fallback (gated) ────────────────────────────────────────────────────
# When NO structured tool fits, the agent can drop down to the raw Revit API by writing a
# small C# snippet that runs against the live Document (mcp-servers' send_code_to_revit).
# This is the one capability that used to be hard-blocked; it is now available but GATED
# (EXECUTOR_ALLOW_API_FALLBACK, default on) so it can be disabled for safe-mode demos, it is
# transactional+undoable by default, and the generated code is streamed to the user for
# oversight. It is a LAST RESORT, not the primary path.
API_FALLBACK_ENABLED = os.environ.get("EXECUTOR_ALLOW_API_FALLBACK", "1").lower() not in ("0", "false", "no", "")

EXECUTE_API_TOOL: dict = {
    "name": "execute_revit_api",
    "description": (
        "LAST-RESORT fallback — run a small C# snippet against the live Revit API when NO "
        "structured tool can do the step (a capability the backend doesn't expose as a tool). "
        "Your code is the BODY of `object Execute(Document document, object[] parameters)`: "
        "`document` is the active document; `return` a JSON-serializable value (ids, counts, "
        "strings). In scope: Autodesk.Revit.DB, Autodesk.Revit.UI, System, System.Linq, "
        "System.Collections.Generic. transactionMode 'auto' (default) wraps your code in ONE "
        "undoable Revit Transaction that rolls back on error — use it for writes; use 'none' "
        "for READ-ONLY queries (no transaction). RULES: try the structured tools FIRST; read "
        "before you write; keep the snippet minimal and scoped to the goal; NEVER delete or "
        "modify anything the goal did not ask for; always fill 'purpose'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "purpose": {"type": "string",
                        "description": "One line: what this code does and why no structured tool fit."},
            "code": {"type": "string",
                     "description": "C# body of Execute(Document document, object[] parameters); must return a value."},
            "transactionMode": {"type": "string", "enum": ["auto", "none"],
                                "description": "auto = wrap in one undoable Transaction (default, for writes); "
                                               "none = read-only query (no transaction)."},
        },
        "required": ["purpose", "code"],
    },
}

# The full toolset the executor sees = curated ergonomic tools + the entire plugin surface
# (+ the gated Revit API fallback). send_code_to_revit is never exposed by name; the fallback
# is the only sanctioned path to it, and only when enabled.
TOOL_SCHEMAS: list[dict] = (
    CURATED_SCHEMAS + revit_tools.TOOL_SCHEMAS + ([EXECUTE_API_TOOL] if API_FALLBACK_ENABLED else [])
)
ALLOWED_TOOLS = {t["name"] for t in TOOL_SCHEMAS}

EXECUTOR_SYSTEM = """\
You are the execution layer of a BIM personalization assistant. You replay a LEARNED Revit
routine in the user's LIVE model using the tools. Work one step at a time.

LEARN FROM THE MODEL — QUERY MISSING INFORMATION BEFORE YOU GUESS:
The routine was learned offline and is blind to THIS model. When a fact you need is missing or
uncertain, find it out from the live model with the read tools instead of guessing or failing:
- Unsure the routine's family is loaded here? Call get_available_family_types for the category
  FIRST and place the family that actually exists — don't place blind and wait for "created 0".
- About to place a wall-hosted door/window? Call inspect_model and check the Walls count. If it
  is 0 there is nothing to host on — say so and ask the user to draw or pick a wall; do NOT loop.
- Need a host wall or a target element? Call get_selected_elements — if the user pre-selected a
  wall, use its id as host_wall_id (that is the user telling you where).
- Unsure where you are? Call get_active_view (point placement needs a plan or 3D view).
Read tools are cheap and safe — prefer one query over a failed write or a question to the user.
Once you've learned a fact (which family is loaded, which wall, which level), reuse it; don't
re-query the same thing every step.

YOUR OTHER JOB IS TO SELF-CORRECT ON ERRORS:
- Every tool result has "success" and a "message". If a tool FAILS, read the message,
  diagnose the cause, and RETRY with a fix. Never give up after a single failure.
- "no valid host" / "no host found": the element is wall-hosted (door/window) and there is
  no wall at that point. Recover: call pick_point (tell the user to click ON a wall), then
  retry place_element at that point — or pass host_wall_id if you know a wall.
- "created 0" / "type not found" / "family not loaded": call get_available_family_types for
  the category, pick the CLOSEST loaded family to what the routine asked for, and retry.
- After 3 failed attempts on the SAME step, stop and explain what you tried and why you're stuck.

Apply the routine's parameters and tag once the element is placed. When the routine is fully
done (placed + parameters set + tagged), reply with a SHORT plain-text summary and DO NOT call
any more tools. Be concise; the user is watching your steps.

YOU HAVE THE FULL REVIT BACKEND, not just place/set/tag. Beyond the curated tools you can also
create walls, floors, grids, levels, rooms, structural framing and dimensions; color/override,
duplicate, or delete elements; run atomic transaction groups; and query the model in depth
(element parameters & definitions, element info, material quantities, room data, view elements,
ai_element_filter). Prefer the simple curated tools for the common Place→SetParam→Tag routine;
reach for the advanced tools when the goal needs them or to gather context. All geometry is in
MILLIMETRES. Tools marked DESTRUCTIVE (delete_element, operate_element, execute_transaction_group)
change or remove existing work — use them only when the goal clearly asks for it.

REVIT API FALLBACK (execute_revit_api) — your escape hatch when the backend has no tool for the
step. If, after checking, NO structured tool can do what the goal needs, write a small C# snippet
against the live Revit API instead of giving up. Discipline: (1) exhaust the structured tools
first — the fallback is a last resort, not a shortcut; (2) for anything you're unsure about,
query READ-ONLY first (transactionMode 'none') to learn the model, THEN write; (3) keep snippets
minimal and scoped strictly to the goal; (4) writes run in one undoable transaction — never touch
elements the goal didn't ask about; (5) say in 'purpose' what the code does. If the fallback is
disabled you'll be told — then explain what you'd need instead.
"""


# ── Real Revit dispatch (each tool → revit_bridge plugin call) ────────────────────

def _ok(res: Any) -> bool:
    return isinstance(res, dict) and bool(res.get("Success"))


def _msg(res: Any) -> str:
    if isinstance(res, dict):
        return res.get("Message") or res.get("error") or ""
    return str(res)


def real_dispatch(name: str, args: dict) -> dict:
    """Execute one tool against the live Revit plugin. Returns a normalized result."""
    from mcp_server import revit_bridge as rb

    if name == "place_element":
        loc = args.get("location", {})
        data: dict = {
            "name": args["family_name"],
            "locationPoint": {"x": loc.get("x", 0), "y": loc.get("y", 0), "z": loc.get("z", 0)},
            "baseLevel": 0, "baseOffset": 0,
        }
        if args.get("type_name"):
            data["typeName"] = args["type_name"]
        if args.get("host_wall_id"):
            data["hostWallId"] = int(args["host_wall_id"])
        res = rb._call_plugin("create_point_based_element", {"data": [data]})
        eid = rb._extract_element_id(res) if isinstance(res, dict) else None
        if _ok(res) and eid:
            return {"success": True, "message": "placed", "element_id": eid}
        return {"success": False, "message": _msg(res) or "no element created (no host or family not loaded)"}

    if name == "set_parameter":
        res = rb._call_plugin("set_element_parameter", {
            "elementId": int(args["element_id"]),
            "parameters": [{"name": args["name"], "value": args["value"]}],
        })
        return {"success": _ok(res), "message": _msg(res) or "parameter set"}

    if name == "tag_element":
        res = rb._call_plugin("tag_element", {
            "elementId": int(args["element_id"]), "useLeader": False, "offsetX": 0.0, "offsetY": 0.0,
        })
        return {"success": _ok(res), "message": _msg(res) or "tagged",
                "tag_id": rb._extract_element_id(res) if isinstance(res, dict) else None}

    if name == "get_available_family_types":
        res = rb._call_plugin("get_available_family_types", {"categoryList": [args["category"]]})
        rows = res if isinstance(res, list) else []
        types = [{"family": d.get("FamilyName"), "type": d.get("TypeName"), "id": d.get("FamilyTypeId")}
                 for d in rows]
        return {"success": True, "message": f"{len(types)} type(s) loaded", "types": types[:60]}

    if name == "get_active_view":
        res = rb._call_plugin("get_current_view_info", {})
        if isinstance(res, dict):
            return {"success": True, "view": {"name": res.get("Name"), "type": res.get("ViewType"),
                                              "scale": res.get("Scale")}}
        return {"success": False, "message": _msg(res)}

    if name == "inspect_model":
        res = rb._call_plugin("analyze_model_statistics", {})
        cats = ({c.get("categoryName"): c.get("elementCount") for c in (res.get("categories") or [])}
                if isinstance(res, dict) else {})
        keep = {k: cats[k] for k in ("Walls", "Doors", "Windows", "Floors", "Levels", "Rooms") if k in cats}
        return {"success": True, "counts": keep, "total_categories": len(cats)}

    if name == "get_selected_elements":
        res = rb._call_plugin("get_selected_elements", {})
        ids = []
        if isinstance(res, list):
            for e in res:
                eid = e.get("Id") or e.get("ElementId") or (e.get("Properties") or {}).get("ElementId")
                if eid is not None:
                    ids.append(eid)
        return {"success": True, "selected_ids": ids}

    if name == "pick_point":
        res = rb.pick_point("point", args.get("prompt", "Click the placement point — on a wall for a door/window."))
        if _ok(res):
            r = res.get("Response") or {}
            return {"success": True, "message": "picked", "location": {"x": r.get("x"), "y": r.get("y"), "z": r.get("z")}}
        return {"success": False, "message": _msg(res) or "pick cancelled"}

    # Gated Revit API fallback — compile + run a C# snippet against the live Document via the
    # plugin's send_code_to_revit. Transactional (auto) so a bad snippet rolls back + is undoable.
    if name == "execute_revit_api":
        if not API_FALLBACK_ENABLED:
            return {"success": False, "message": "Revit API fallback is disabled "
                                                 "(set EXECUTOR_ALLOW_API_FALLBACK=1 to enable)."}
        code = (args.get("code") or "").strip()
        if not code:
            return {"success": False, "message": "no code provided"}
        mode = "none" if str(args.get("transactionMode", "auto")).lower() == "none" else "auto"
        res = rb._call_plugin("send_code_to_revit", {"code": code, "transactionMode": mode}, timeout=90)
        if isinstance(res, dict):
            if "error" in res:
                return {"success": False, "message": str(res["error"])}
            ok = bool(res.get("success"))
            return {"success": ok,
                    "message": ("executed" if ok else (res.get("errorMessage") or "code failed")),
                    "result": res.get("result")}
        return {"success": False, "message": "unexpected response from send_code_to_revit"}

    # Full plugin surface — every other exposed mcp-servers-for-revit command is dispatched
    # generically (its args ARE the plugin params). send_code_to_revit is not in this set.
    if name in revit_tools.TOOL_NAMES:
        return revit_tools.dispatch(name, args)

    return {"success": False, "message": f"unknown or disallowed tool: {name}"}


# ── The agentic loop ──────────────────────────────────────────────────────────────

def _blocks(resp: Any) -> list:
    return list(getattr(resp, "content", []) or [])


def _text_of(blocks: list) -> str:
    return " ".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text").strip()


def run_executor(
    goal: str,
    *,
    client: Any = None,
    dispatch_fn: Callable[[str, dict], dict] = real_dispatch,
    on_event: Callable[[str, Any], None] | None = None,
    max_iters: int = MAX_ITERS,
    model: str = EXECUTOR_MODEL,
    memory_block: str = "",
) -> dict:
    """Run the self-healing execution loop. Returns
       {done, summary, steps, attempts, tool_calls:[{name,args,result}]}.

    `client` defaults to a fresh Anthropic client; inject a fake for testing.
    `dispatch_fn` runs a tool in Revit; inject a fake to test without a live model.
    `on_event(kind, payload)` streams progress: kind ∈ {reasoning, tool, result, done, error}.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    emit = on_event or (lambda *_: None)
    system = EXECUTOR_SYSTEM + (memory_block or "")   # project memory steers the run
    messages: list[dict] = [{"role": "user", "content": goal}]
    tool_calls: list[dict] = []
    attempts = 0

    for _ in range(max_iters):
        try:
            resp = client.messages.create(
                model=model, max_tokens=1024, system=system,
                tools=TOOL_SCHEMAS, messages=messages,
            )
        except Exception as exc:
            emit("error", str(exc))
            return {"done": False, "summary": f"Executor error: {exc}",
                    "attempts": attempts, "tool_calls": tool_calls}

        blocks = _blocks(resp)
        text = _text_of(blocks)
        if text:
            emit("reasoning", text)

        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        # record the assistant turn verbatim so the tool_use ids line up
        messages.append({"role": "assistant", "content": blocks})

        if not tool_uses:
            emit("done", text)
            return {"done": True, "summary": text or "Routine complete.",
                    "attempts": attempts, "tool_calls": tool_calls}

        results_content = []
        for tu in tool_uses:
            name, args, tid = tu.name, (tu.input or {}), tu.id
            attempts += 1
            if name not in ALLOWED_TOOLS:
                result = {"success": False, "message": f"tool '{name}' is not allowed"}
            else:
                emit("tool", {"name": name, "args": args})
                try:
                    result = dispatch_fn(name, args)
                except Exception as exc:
                    result = {"success": False, "message": f"dispatch raised: {exc}"}
                emit("result", {"name": name, "result": result})
            tool_calls.append({"name": name, "args": args, "result": result})
            results_content.append({
                "type": "tool_result", "tool_use_id": tid,
                "content": json.dumps(result),
                "is_error": not bool(result.get("success", False)),
            })

        messages.append({"role": "user", "content": results_content})

    emit("error", "iteration cap reached")
    return {"done": False, "summary": "Reached the step cap before finishing.",
            "attempts": attempts, "tool_calls": tool_calls}


def build_goal(motif: dict, location: dict | None = None) -> str:
    """Turn a detected routine (motif + steps) into the executor's goal prompt."""
    steps = motif.get("steps", []) if isinstance(motif, dict) else []
    lines = [f"Routine: {motif.get('name', 'Detected routine')}", "Steps to reproduce:"]
    for i, s in enumerate(steps, 1):
        action = s.get("action", "?")
        ft = s.get("family_type", "")
        pn = s.get("param_name", "")
        pv = s.get("param_value", "")
        if ft:
            lines.append(f"  {i}. {action}: {ft}")
        elif pn:
            lines.append(f"  {i}. {action}: {pn} = {pv}")
        else:
            lines.append(f"  {i}. {action}")
    if location:
        lines.append(f"Place it at approximately x={location.get('x')}, y={location.get('y')} (mm). "
                     f"If it needs a host wall and none is there, ask the user to pick a point on a wall.")
    else:
        lines.append("No location given yet — use pick_point to have the user click where to place it.")
    lines.append("Reproduce the routine now, self-correcting on any tool error.")
    return "\n".join(lines)


def _load_api_key() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    for env in (Path(__file__).resolve().parent.parent / ".env",):
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return
        except Exception:
            pass


if __name__ == "__main__":
    # Manual live run against the real Revit plugin (needs Revit + :8080 + ANTHROPIC_API_KEY).
    import sys
    _load_api_key()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    demo_motif = {"name": "Place Door + Tag + Mark", "steps": [
        {"action": "Place", "family_type": "M_Single-Flush"},
        {"action": "Tag", "family_type": "Door Tag"},
        {"action": "SetParam", "param_name": "Mark", "param_value": "D-EXEC"},
    ]}
    def _print(kind, payload):
        print(f"[{kind}] {payload}")
    out = run_executor(build_goal(demo_motif), on_event=_print)
    print("\nRESULT:", json.dumps({k: v for k, v in out.items() if k != "tool_calls"}, indent=2))
