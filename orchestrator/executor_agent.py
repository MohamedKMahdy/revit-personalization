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
import re
from pathlib import Path
from typing import Any, Callable

from orchestrator import revit_tools
from shared import llm

EXECUTOR_MODEL = llm.pick("EXECUTOR_MODEL", "claude-sonnet-4-6")
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

# COST: the tool block (~30 verbose schemas) is identical on every call and dominates the input
# tokens. A cache_control breakpoint on the LAST tool lets the API serve the whole tools+system
# prefix from cache on repeat calls (~0.1x price) instead of re-billing every schema on every loop
# iteration. Measured: the prefix is ~19.8K tokens; a warm Sonnet call reads all of it from cache.
#
# TTL: the default ephemeral cache is 5 MINUTES. This loop can stall far longer than that on a
# pick_point — the USER has to click in Revit, which can take minutes — so the prefix would expire
# mid-run and get re-billed at full price on the next iteration. We use the 1-HOUR extended TTL so
# the prefix survives those waits; the only cost is a slightly dearer one-time write (~2x vs 1.25x).
# Set EXECUTOR_CACHE_TTL="" (or "5m") to fall back to the default 5-minute cache. (Copy the last
# dict so we don't mutate the shared revit_tools/EXECUTE_API_TOOL definitions.)
_CACHE_TTL = os.environ.get("EXECUTOR_CACHE_TTL", "1h").strip()
_EXTENDED_TTL = _CACHE_TTL not in ("", "5m")           # 1h needs the extended-cache-ttl beta header
_CACHE_BETA_HEADER = {"anthropic-beta": "extended-cache-ttl-2025-04-11"}


def _cache_control() -> dict:
    cc = {"type": "ephemeral"}
    if _CACHE_TTL:
        cc["ttl"] = _CACHE_TTL
    return cc


if TOOL_SCHEMAS:
    TOOL_SCHEMAS[-1] = {**TOOL_SCHEMAS[-1], "cache_control": _cache_control()}

# Cache-free copy of the tool schemas for backends that don't support Anthropic prompt caching
# (Gemini via the LiteLLM proxy) — sending cache_control there is at best ignored, at worst a 400.
TOOL_SCHEMAS_PLAIN: list[dict] = [{k: v for k, v in t.items() if k != "cache_control"}
                                 for t in TOOL_SCHEMAS]

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
- "created 0" / "type not found" / "family not loaded": FIRST call get_available_family_types for
  the category — if the family is genuinely NOT loaded, pick the closest loaded one and retry.
- HOSTED FAMILIES (doors & windows) — IMPORTANT, READ THIS: place_element / create_point_based_element
  CANNOT place a wall-hosted family. It returns "success, created 0 element(s)" and places NOTHING,
  even with the correct loaded family, a valid point on the wall, and host_wall_id. This is a REAL
  capability gap, NOT a recoverable structured-tool error. So: if the family is a door or window
  and a place attempt returns "created 0" while the family IS loaded, do NOT keep retrying
  place_element and do NOT keep re-picking points — go straight to execute_revit_api and place it
  with the Revit API. Exact pattern (fill in the family name, host wall id, and mm point on the wall):
      var wall = document.GetElement(new ElementId(HOST_WALL_ID)) as Autodesk.Revit.DB.Wall;
      var level = document.GetElement(wall.LevelId) as Autodesk.Revit.DB.Level;
      var sym = new FilteredElementCollector(document).OfClass(typeof(FamilySymbol))
          .Cast<FamilySymbol>().FirstOrDefault(s => s.FamilyName == "FAMILY_NAME");
      if (sym == null) return "symbol not found";
      if (!sym.IsActive) sym.Activate();
      var pt = new XYZ(X_MM/304.8, Y_MM/304.8, 0);   // a point ON the wall (use its centerline X/Y)
      var inst = document.Create.NewFamilyInstance(pt, sym, wall, level,
          Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
      return inst == null ? "null" : inst.Id.ToString();
  Get HOST_WALL_ID from get_selected_elements or by querying the model for a wall; get the wall's
  centerline X/Y from its bounding box (the door sits on the wall). Then set parameters + tag the
  returned id normally. (For NON-hosted families — furniture, generic models — place_element works.)
- For NON-hosted placements, a structured tool returning an error is EXPECTED and recoverable — stay
  on the structured tools and fix the call. Do NOT respond to a non-hosted place/set/tag/create
  failure by switching to the Revit API
  fallback; that tool is only for operations no structured tool supports (see below).
- After 3 failed attempts on the SAME step, stop and explain what you tried and why you're stuck.
- If the memory block below contains a "WHAT WENT WRONG BEFORE ON THIS ROUTINE" section, treat it as
  authoritative: those are mistakes you actually made on prior runs. Apply each fix on your FIRST
  action — do NOT repeat the failed approach just to rediscover it. E.g. if it says this family is
  wall-hosted, get a host wall id (get_selected_elements / pick_point) and use place_and_configure
  with it BEFORE any bare place_element.

FINISH THE WHOLE ROUTINE — a placement ALONE is never "done". After you place the element you MUST
continue: set EVERY parameter the routine lists (set_parameter on the placed element's id) and tag
it if the routine tags. Do not end your turn, and do not write a summary, until every step has a
SUCCESSFUL tool result. If you just placed something and haven't set its parameters or tagged it,
your next action is a tool call, not a stopping point. Only when placed + all parameters set + tagged
do you reply with a SHORT plain-text summary and stop. Be concise; the user is watching your steps.

YOU HAVE THE FULL REVIT BACKEND, not just place/set/tag. Beyond the curated tools you can also
create walls, floors, grids, levels, rooms, structural framing and dimensions; color/override,
duplicate, or delete elements; run atomic transaction groups; and query the model in depth
(element parameters & definitions, element info, material quantities, room data, view elements,
ai_element_filter). Prefer the simple curated tools for the common Place→SetParam→Tag routine;
reach for the advanced tools when the goal needs them or to gather context. All geometry is in
MILLIMETRES. Tools marked DESTRUCTIVE (delete_element, operate_element, execute_transaction_group)
change or remove existing work — use them only when the goal clearly asks for it.

REVIT API FALLBACK (execute_revit_api) — for a MISSING CAPABILITY, not a failed tool.
Draw this line sharply:
  • A structured tool EXISTS for what you're doing (place_element, set_parameter, tag_element,
    create_*, pick_point, the query tools…) but it returned an error → this is NORMAL. It is NOT
    a missing capability. Do NOT switch to execute_revit_api. Diagnose and RETRY with the
    structured tools: pick a different point/host (pick_point), choose a loaded family
    (get_available_family_types), fix the parameter value, query the model with the read tools.
  • There is genuinely NO structured tool for the operation (e.g. rename a view, join two walls,
    set a project-information parameter, change a workset) → only THEN write a small C# snippet.
A tool failing is your cue to fix the structured call, never your cue to drop to the API. Reaching
for execute_revit_api just because place/set/tag failed is the wrong move — the user does not want
raw API used in place of the tools that already do the job. When you DO use it (true gap only):
query READ-ONLY first (transactionMode 'none') if unsure; keep the snippet minimal and scoped to
the goal; writes run in one undoable transaction — never touch elements the goal didn't ask about;
state in 'purpose' which structured tool you tried and why it cannot express this. If it's disabled
you'll be told — then explain what you'd need instead.
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


def needs_confirmation(name: str, args: dict) -> bool:
    """Which tool calls must pause for an explicit user OK before running. Today: any
    Revit API fallback that WRITES (transactionMode != 'none') — read-only queries run free."""
    if name == "execute_revit_api":
        return str((args or {}).get("transactionMode", "auto")).lower() != "none"
    return False


def _hit_hosted_placement_gap(tool_calls: list[dict]) -> bool:
    """True if a placement attempt already returned the 'created 0 / no element' signature. The
    structured placement (create_point_based_element) CANNOT host doors/windows — it silently creates
    nothing — so once that's happened, dropping to execute_revit_api (NewFamilyInstance + host) is the
    LEGITIMATE recovery and the API nudge should not block it."""
    for c in tool_calls:
        if c.get("name") in PLACE_TOOLS:
            msg = ((c.get("result") or {}).get("message") or "").lower()
            if "created 0" in msg or "0 element" in msg or "no element" in msg:
                return True
    return False


# Programmatic brake against treating a structured-tool failure as a reason to drop to raw API.
# The FIRST time the agent reaches for execute_revit_api after working with structured tools, this
# nudge is returned instead of running it; the agent must reaffirm (call it again) to proceed — so
# a genuine capability gap still gets through, but a knee-jerk escalation gets redirected.
API_NUDGE = (
    "Not yet — execute_revit_api is ONLY for operations NO structured tool supports. A structured "
    "tool failing is not a missing capability. Re-check the tools and retry the structured way: "
    "re-pick the host (pick_point), choose a loaded family (get_available_family_types), correct the "
    "parameter, or query the model with the read tools. If — and only if — NO structured tool can "
    "express THIS specific operation (e.g. rename a view, join walls, set a project parameter), call "
    "execute_revit_api again with a 'purpose' naming the structured tool you tried, and it will run."
)


# Completion enforcement — the routine isn't "done" just because the model stopped calling tools.
# A weaker model (e.g. Gemini Flash) tends to place the element and then declare success without
# setting parameters or tagging. We compute the routine's required steps, and if the model stops
# early we re-prompt it to finish; if it STILL won't, we complete the known steps deterministically.
MAX_COMPLETION_NUDGES = 2


# Any tool that PLACES an element / SETS a parameter / TAGS — the agent has several ways to do each
# (e.g. place_and_configure both places AND sets parameters in one atomic call), so completion can't
# key off place_element/set_parameter/tag_element alone.
PLACE_TOOLS = {"place_element", "place_and_configure", "create_point_based_element",
               "create_line_based_element", "create_surface_based_element", "duplicate_element"}
SETPARAM_TOOLS = {"set_parameter", "set_element_parameter"}
TAG_TOOLS = {"tag_element", "tag_walls", "tag_rooms"}


def next_in_sequence(value):
    """The next value in a sequence: increment the LAST run of digits, preserving prefix/suffix and
    zero-padding. 'D-105'->'D-106', '1002'->'1003', 'W-09'->'W-10', 'ABC'->None (no number)."""
    if value in (None, ""):
        return None
    s = str(value)
    m = re.search(r"(\d+)(\D*)$", s)
    if not m:
        return None
    num, suffix = m.group(1), m.group(2)
    nxt = str(int(num) + 1).zfill(len(num))            # zfill keeps width and never truncates
    return s[:m.start(1)] + nxt + suffix


def _max_in_sequence(values: list) -> str | None:
    """The value with the highest trailing number (where the sequence currently sits)."""
    best, best_n = None, None
    for v in values:
        m = re.search(r"(\d+)(\D*)$", str(v))
        if not m:
            continue
        n = int(m.group(1))
        if best_n is None or n > best_n:
            best, best_n = str(v), n
    return best


def resolve_routine_values(motif: dict, examples: list | None = None,
                           last_values: dict | None = None) -> dict:
    """Decide the value to USE for each parameter the routine sets. A constant value stays; a
    variable one (e.g. Mark) becomes the NEXT in its observed sequence — incremented from the value
    we last set (project memory), or from the highest value seen in the recorded examples."""
    examples = examples or []
    last_values = last_values or {}

    def _clean(v):
        return None if v is None or str(v).strip().lower() in ("", "none", "null") else v

    out: dict = {}
    for s in (motif.get("steps", []) if isinstance(motif, dict) else []):
        pn = s.get("param_name")
        if not pn:
            continue
        ptype = (s.get("param_value_type") or "").lower()
        pv = _clean(s.get("param_value"))
        if pv is not None and ptype != "variable":
            out[pn] = pv                                # constant — use as recorded
            continue
        # variable → next in sequence: prefer the value we set last time, else the highest seen in
        # the recorded examples (and skip junk like a stray "None" from a past malformed run).
        nxt = next_in_sequence(_clean(last_values.get(pn)))
        if nxt is None:
            seen = [_clean(a.get("param_value_after") or a.get("param_value"))
                    for e in examples for a in (e.get("actions") or [])
                    if a.get("param_name") == pn]
            nxt = next_in_sequence(_max_in_sequence([v for v in seen if v is not None]))
        if nxt is not None:
            out[pn] = nxt
    return out


def required_steps_from_motif(motif: dict, param_values: dict | None = None) -> list[dict]:
    """The must-happen steps of a routine, for completion enforcement: place / set_parameter / tag.
    Reads the Pattern Agent's actual fields (action_type, family_name, …) with legacy fallbacks.
    `param_values` (from resolve_routine_values) fills in the concrete value for each parameter."""
    steps = motif.get("steps", []) if isinstance(motif, dict) else []
    pvals = param_values or {}
    out: list[dict] = []
    for s in steps:
        a = (s.get("action_type") or s.get("action") or "").lower()
        if "place" in a or "create" in a:
            out.append({"type": "place"})
        elif "tag" in a:
            out.append({"type": "tag"})
        elif "param" in a or s.get("param_name"):
            pn = s.get("param_name")
            out.append({"type": "set_parameter", "name": pn,
                        "value": pvals.get(pn, s.get("param_value"))})
    return out


def _ok_calls(tool_calls: list[dict]) -> list[dict]:
    return [c for c in tool_calls if (c.get("result") or {}).get("success")]


def _result_element_id(result: dict):
    """Pull a created element id from any placement result — curated (element_id) or generic (response)."""
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
    if isinstance(resp, (int, str)):
        try:
            return int(resp)
        except (TypeError, ValueError):
            pass
    return None


def placed_element_id(tool_calls: list[dict]):
    """The id of the most-recently placed element, from ANY placement tool."""
    for c in reversed(tool_calls):
        if c.get("name") in PLACE_TOOLS and (c.get("result") or {}).get("success"):
            eid = _result_element_id(c.get("result") or {})
            if eid is not None:
                return eid
    return None


def _incomplete_steps(required: list[dict] | None, tool_calls: list[dict]) -> list[dict]:
    """Required steps with no matching successful tool call yet. Empty when complete / unconstrained."""
    if not required:
        return []
    ok = _ok_calls(tool_calls)
    placed = any(c["name"] in PLACE_TOOLS for c in ok)
    configured = any(c["name"] == "place_and_configure" for c in ok)   # places AND sets params
    set_names = {(c["args"].get("name") or "").lower() for c in ok if c["name"] in SETPARAM_TOOLS}
    tagged = any(c["name"] in TAG_TOOLS for c in ok)
    missing = []
    for step in required:
        t = step.get("type")
        if t == "place" and not placed:
            missing.append(step)
        elif t == "set_parameter" and not configured and (step.get("name") or "").lower() not in set_names:
            missing.append(step)
        elif t == "tag" and not tagged:
            missing.append(step)
    return missing


def _completion_nudge(missing: list[dict], placed_id) -> str:
    parts = []
    for s in missing:
        if s["type"] == "set_parameter":
            parts.append(f"set parameter '{s.get('name')}' = '{s.get('value')}'")
        elif s["type"] == "tag":
            parts.append("tag the element")
        elif s["type"] == "place":
            parts.append("place the element")
    el = f" (the placed element id is {placed_id})" if placed_id else ""
    return ("The routine is NOT finished yet" + el + ". You still need to: " + "; ".join(parts)
            + ". Do it now with the tools — a placement alone is never done; do not stop until every "
              "step has a successful tool result.")


# ── Adaptive model escalation (cost) ───────────────────────────────────────────────
# Start a KNOWN, simple routine on a cheap model (Haiku ~3x cheaper than Sonnet) and only escalate
# to the configured ceiling if it struggles. Cold/novel/complex routines start on the ceiling. This
# is only worthwhile when the ceiling is a PAID Claude model — if the executor is on Gemini (free),
# starting on paid Haiku would cost MORE, so adaptive is skipped.
ADAPTIVE_START = os.environ.get("EXECUTOR_ADAPTIVE", "1").lower() not in ("0", "false", "no", "")
CHEAP_MODEL = llm.resolve(os.environ.get("EXECUTOR_CHEAP_MODEL", "haiku"))
ESCALATE_AFTER_FAILURES = int(os.environ.get("EXECUTOR_ESCALATE_AFTER_FAILURES", "2"))


def _is_simple_motif(motif: dict) -> bool:
    """A routine the cheap model can likely handle: only place / set-parameter / tag / create steps."""
    steps = motif.get("steps", []) if isinstance(motif, dict) else []
    if not steps:
        return False
    for s in steps:
        a = (s.get("action_type") or s.get("action") or "").lower()
        if not (any(t in a for t in ("place", "set", "param", "tag", "create")) or s.get("param_name")):
            return False
    return True


def choose_start_model(motif: dict, routine_entry: dict | None) -> tuple[str, str | None]:
    """(start_model, escalate_to). For a memory-WARM, simple routine on a paid ceiling, start cheap
    (Haiku) and escalate to the ceiling on difficulty; otherwise start on the ceiling with no
    escalation. routine_entry is project_memory's per-routine dict (executions / known host / subs)."""
    ceiling = EXECUTOR_MODEL
    if not ADAPTIVE_START or llm.is_gemini(ceiling) or llm.resolve(ceiling) == CHEAP_MODEL:
        return ceiling, None                              # free tier, disabled, or already cheapest
    r = routine_entry or {}
    warm = bool(r.get("executions", 0) or r.get("last_host_wall_id") or r.get("family_substitutions"))
    if warm and _is_simple_motif(motif):
        return CHEAP_MODEL, ceiling
    return ceiling, None


def _preflight_facts(dispatch_fn: Callable[[str, dict], dict],
                     emit: Callable[[str, Any], None]) -> str:
    """Best-effort READ-ONLY grounding before the agentic loop: read the user's current Revit
    selection once and hand it to the model, so it leads with the right host instead of spending a
    model round-trip discovering it (or placing blind). Never raises; returns "" if nothing useful."""
    try:
        sel = dispatch_fn("get_selected_elements", {}) or {}
    except Exception:
        return ""
    ids = sel.get("selected_ids") or []
    if not ids:
        return ""
    emit("reasoning", f"Pre-flight: you have {len(ids)} element(s) selected in Revit.")
    return ("LIVE CONTEXT (read from Revit before you start): the user currently has element id(s) "
            f"{ids} selected. If a step needs a host wall (a door/window), use the selected WALL's id "
            "as host_wall_id and place via place_and_configure — do NOT call pick_point or place "
            "blind. If a selected id is not a wall, ignore it for hosting.")


def run_executor(
    goal: str,
    *,
    client: Any = None,
    dispatch_fn: Callable[[str, dict], dict] = real_dispatch,
    on_event: Callable[[str, Any], None] | None = None,
    confirm_fn: Callable[[str, dict], bool] | None = None,
    guard_api_fallback: bool = True,
    required: list[dict] | None = None,
    max_iters: int = MAX_ITERS,
    model: str = EXECUTOR_MODEL,
    memory_block: str = "",
    escalate_to: str | None = None,
    escalate_after_failures: int = ESCALATE_AFTER_FAILURES,
    preflight: bool = True,
) -> dict:
    """Run the self-healing execution loop. Returns
       {done, summary, steps, attempts, tool_calls:[{name,args,result}]}.

    `client` defaults to a fresh Anthropic client; inject a fake for testing.
    `dispatch_fn` runs a tool in Revit; inject a fake to test without a live model.
    `on_event(kind, payload)` streams progress: kind ∈ {reasoning, tool, result, done, error}.
    `confirm_fn(name, args) -> bool` is consulted before dispatching a confirmation-gated tool
    (see needs_confirmation); returning False blocks the call with a 'user declined' result.
    """
    model = llm.resolve(model)                       # alias (sonnet/opus/gemini) → concrete id
    if client is None:
        client = llm.client(model)                   # direct to Anthropic, or via the Gemini proxy

    emit = on_event or (lambda *_: None)
    system = EXECUTOR_SYSTEM + (memory_block or "")   # project memory steers the run
    # Cache the tools+system prefix on Claude (iterations 2+ read it at ~0.1x). Gemini can't cache,
    # so send the plain string + cache-free tools there.
    cache = llm.supports_prompt_caching(model)
    system_param = ([{"type": "text", "text": system, "cache_control": _cache_control()}]
                    if cache else system)
    tools_param = TOOL_SCHEMAS if cache else TOOL_SCHEMAS_PLAIN
    # The 1-hour TTL needs the extended-cache beta header (only when actually caching on Claude).
    extra_headers = _CACHE_BETA_HEADER if (cache and _EXTENDED_TTL) else None

    # PRE-FLIGHT: read the live Revit selection once and prepend it to the goal, so the model leads
    # with the correct host instead of burning a round-trip discovering it (or placing blind).
    if preflight:
        facts = _preflight_facts(dispatch_fn, emit)
        if facts:
            goal = facts + "\n\n" + goal
    messages: list[dict] = [{"role": "user", "content": goal}]
    tool_calls: list[dict] = []
    attempts = 0
    api_reaffirmed = False     # has the agent reaffirmed an API-fallback escalation this turn?
    completion_nudges = 0      # times we've re-prompted the model to finish unfinished routine steps
    escalated = False          # have we stepped the cheap start model up to the ceiling this run?
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    for _ in range(max_iters):
        try:
            resp = client.messages.create(
                model=model, max_tokens=1024, system=system_param,
                tools=tools_param, messages=messages,
                **({"extra_headers": extra_headers} if extra_headers else {}),
            )
        except Exception as exc:
            emit("error", str(exc))
            return {"done": False, "summary": f"Executor error: {exc}",
                    "attempts": attempts, "tool_calls": tool_calls, "usage": usage, "model": model,
                    "nudged": completion_nudges, "escalated": escalated}

        u = getattr(resp, "usage", None)
        if u is not None:
            usage["input"] += getattr(u, "input_tokens", 0) or 0
            usage["output"] += getattr(u, "output_tokens", 0) or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        blocks = _blocks(resp)
        text = _text_of(blocks)
        if text:
            emit("reasoning", text)

        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        # record the assistant turn verbatim so the tool_use ids line up
        messages.append({"role": "assistant", "content": blocks})

        if not tool_uses:
            missing = _incomplete_steps(required, tool_calls)
            if missing:
                placed_id = placed_element_id(tool_calls)
                if completion_nudges < MAX_COMPLETION_NUDGES:
                    # The model stopped with routine steps unfinished — push it to complete them.
                    completion_nudges += 1
                    emit("reasoning", "Routine not finished — continuing the remaining steps.")
                    messages.append({"role": "user", "content": _completion_nudge(missing, placed_id)})
                    continue
                # The model won't finish (e.g. a weaker model). Complete the KNOWN steps ourselves on
                # the placed element so the routine doesn't end half-done.
                if placed_id:
                    for s in missing:
                        if s["type"] == "set_parameter":
                            if s.get("value") in (None, ""):
                                continue   # a variable/runtime param — value unknown, leave it to the user
                            args = {"element_id": placed_id, "name": s.get("name"), "value": s.get("value")}
                            tool = "set_parameter"
                        elif s["type"] == "tag":
                            args, tool = {"element_id": placed_id}, "tag_element"
                        else:
                            continue
                        emit("tool", {"name": tool, "args": args})
                        try:
                            r = dispatch_fn(tool, args)
                        except Exception as exc:
                            r = {"success": False, "message": f"dispatch raised: {exc}"}
                        emit("result", {"name": tool, "result": r})
                        tool_calls.append({"name": tool, "args": args, "result": r})
                    missing = _incomplete_steps(required, tool_calls)
            done = not missing
            emit("done", text)
            return {"done": done, "summary": text or "Routine complete.",
                    "attempts": attempts, "tool_calls": tool_calls, "usage": usage, "model": model,
                    "nudged": completion_nudges, "escalated": escalated}

        results_content = []
        for tu in tool_uses:
            name, args, tid = tu.name, (tu.input or {}), tu.id
            attempts += 1
            if name not in ALLOWED_TOOLS:
                result = {"success": False, "message": f"tool '{name}' is not allowed"}
            else:
                emit("tool", {"name": name, "args": args})
                if (guard_api_fallback and name == "execute_revit_api" and not api_reaffirmed
                        and not _hit_hosted_placement_gap(tool_calls)):
                    # First escalation to raw API this turn — redirect to structured tools once.
                    # EXCEPTION: if a placement already returned "created 0" (the structured tool can't
                    # host this family — doors/windows), dropping to the API IS the legitimate fix, so
                    # don't nudge it (the confirmation gate still applies before any write runs).
                    api_reaffirmed = True
                    result = {"success": False, "message": API_NUDGE}
                elif confirm_fn is not None and needs_confirmation(name, args) and not confirm_fn(name, args):
                    result = {"success": False,
                              "message": "The user declined to run this Revit API code. Do not retry "
                                         "it; use a structured tool instead, or explain what you'd need."}
                else:
                    try:
                        result = dispatch_fn(name, args)
                    except Exception as exc:
                        result = {"success": False, "message": f"dispatch raised: {exc}"}
                emit("result", {"name": name, "result": result})
            # Re-arm the nudge after any non-API tool, so each fresh escalation is challenged once.
            if name != "execute_revit_api":
                api_reaffirmed = False
            tool_calls.append({"name": name, "args": args, "result": result})
            results_content.append({
                "type": "tool_result", "tool_use_id": tid,
                "content": json.dumps(result),
                "is_error": not bool(result.get("success", False)),
            })

        messages.append({"role": "user", "content": results_content})

        # ADAPTIVE ESCALATION: if we started on the cheap model and it's accumulating failures, step
        # up to the ceiling and keep going (same message history; Haiku→Sonnet are both direct-Anthropic
        # so the existing client serves either). Triggers at most once per run.
        if escalate_to and not escalated and llm.resolve(model) != llm.resolve(escalate_to):
            fails = sum(1 for c in tool_calls if not (c.get("result") or {}).get("success"))
            if fails >= escalate_after_failures:
                model = llm.resolve(escalate_to)
                escalated = True
                emit("reasoning", f"Escalating to {model} after {fails} failed attempt(s) on the "
                                  "cheaper model.")

    emit("error", "iteration cap reached")
    return {"done": False, "summary": "Reached the step cap before finishing.",
            "attempts": attempts, "tool_calls": tool_calls, "usage": usage, "model": model,
            "nudged": completion_nudges, "escalated": escalated}


def build_goal(motif: dict, location: dict | None = None, param_values: dict | None = None) -> str:
    """Turn a detected routine (motif + steps) into the executor's goal prompt. Reads the Pattern
    Agent's real step fields (action_type, family_name, tag_family_name, param_name/value).
    `param_values` (from resolve_routine_values) supplies the concrete value for each parameter —
    constants as recorded, variables as the next in sequence (e.g. Mark D-105 -> D-106)."""
    steps = motif.get("steps", []) if isinstance(motif, dict) else []
    pvals = param_values or {}
    lines = [f"Routine: {motif.get('name', 'Detected routine')}", "Steps to reproduce IN ORDER:"]
    for i, s in enumerate(steps, 1):
        a = (s.get("action_type") or s.get("action") or "").strip()
        al = a.lower()
        fam = s.get("family_name") or s.get("family_type") or ""
        pn = s.get("param_name") or ""
        pv = pvals.get(pn, s.get("param_value")) if pn else None
        tagfam = s.get("tag_family_name") or ""
        if ("place" in al or "create" in al) and fam:
            lines.append(f"  {i}. Place the family '{fam}'.")
        elif "tag" in al:
            lines.append(f"  {i}. Tag the placed element" + (f" with '{tagfam}'." if tagfam else "."))
        elif pn:
            if pv in (None, ""):
                lines.append(f"  {i}. Set parameter '{pn}' on the placed element "
                             "(value is provided at runtime — ask the user if you don't have it).")
            else:
                lines.append(f"  {i}. Set parameter '{pn}' = '{pv}' on the placed element "
                             "(this is the next value in the sequence — use it as-is).")
        elif a:
            lines.append(f"  {i}. {a}.")
    if location:
        lines.append(f"Place it at approximately x={location.get('x')}, y={location.get('y')} mm. "
                     "If it needs a host wall and none is there, ask the user to pick a point on a wall.")
    else:
        lines.append("No location given yet — if the user has a wall selected, host on it; otherwise use "
                     "pick_point to have the user click where to place it (on a wall for a door/window).")
    lines.append("Do EVERY step in order — place, set each parameter, and tag — self-correcting on any "
                 "tool error. Do not stop after placing; the routine is only done when all steps succeeded.")
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
