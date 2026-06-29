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

# Per-element reads that must NOT be looped (read them in one batched execute_revit_api instead). After
# this many in a single run, the loop is short-circuited with a nudge — the hard cap on "audit 60 doors
# one at a time" cost blowups (that pattern hit 605K input tokens / ~$2 for a single request).
_READ_LOOP_TOOLS = {"get_element_parameters", "get_element_info", "get_parameter_definitions"}
_READ_LOOP_CAP = int(os.environ.get("EXECUTOR_READ_LOOP_CAP", "8"))

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
                "host_wall_id": {"type": "integer", "description": "Optional explicit host wall element id "
                                 "(for a wall-hosted door/window — the element snaps onto this wall)"},
                "category": {"type": "string", "description": "Optional category hint to speed up family "
                             "resolution, e.g. 'OST_Doors', 'OST_Windows', 'OST_Furniture'"},
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
        "description": "Tag a placed element in the active view. The tag family is resolved "
                       "automatically from the element's category (e.g. a door → its door tag); pass "
                       "tag_type_id only to force a specific tag type. Use offset_x/offset_y (mm) to "
                       "place the tag away from the element for readability (e.g. when the user asks for "
                       "the tag offset from the door/wall).",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
                "tag_type_id": {"type": "integer", "description": "Optional explicit tag FamilyTypeId "
                                "(from get_available_family_types on the tag category, e.g. OST_DoorTags)"},
                "offset_x": {"type": "number", "description": "Tag X offset from the element in mm (default 0)"},
                "offset_y": {"type": "number", "description": "Tag Y offset from the element in mm (default 500, "
                             "so the tag sits clear of the element for readability)"},
            },
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

# Every successful execute_revit_api use = a capability gap the agent had to fill with ad-hoc code.
# We append it to a queue; tools/grow/promote_fallbacks.py later distills each into a clean, parameterized,
# COMPILED bim-mcp command (broken into functions) + a tool schema, so next time there is a real tool.
GROW_CANDIDATES_PATH = Path(__file__).with_name("grow_candidates.jsonl")


def _record_capability_gap(code: str, args: dict) -> None:
    try:
        import time
        rec = {"ts": int(time.time()), "code": code,
               "args": {k: v for k, v in (args or {}).items() if k != "code"}}
        with GROW_CANDIDATES_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

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

# Read-only introspection of Revit's LIVE warning list (duplicate Mark, unhosted element, overlap, …).
# Runs the FIXED, audited snippet below via the same send_code_to_revit path in transactionMode 'none'
# (no transaction, no write) — so the model never authors the code and the call is confirmation-exempt.
# This makes the self-healing executor able to READ Revit's own failure messages instead of being blind
# to warnings that a tool reports success on. (Tier 2 will replace this with a first-class plugin command.)
GET_WARNINGS_CODE = (
    "var ws = document.GetWarnings();\n"
    "return ws.Select(w => new {\n"
    "    description = w.GetDescriptionText(),\n"
    "    severity = w.GetSeverity().ToString(),\n"
    "    has_resolution = w.HasResolutions(),\n"
    "    failing_ids = w.GetFailingElements().Select(id => id.Value).ToList(),\n"
    "    additional_ids = w.GetAdditionalElements().Select(id => id.Value).ToList()\n"
    "}).ToList();"
)

GET_WARNINGS_TOOL: dict = {
    "name": "get_warnings",
    "description": (
        "Read Revit's LIVE warning list (duplicate Mark, unhosted element, overlap, …). Read-only, no "
        "transaction. Call it after a write to check the element you just placed, or anytime a result "
        "looks wrong. Warnings are HINTS, not proof of failure — confirm with a read-back."
    ),
    "input_schema": {"type": "object", "properties": {}},
}


def _bulk_params_code(category: str, param_names: list, in_view_only: bool) -> str:
    """Build a FIXED, audited read snippet: read the given parameters for EVERY element of a category in
    ONE call. The model only supplies a sanitized category + parameter names — it never authors code —
    so this is a read-only, confirmation-exempt batch read (the cure for the 'audit 60 doors one-by-one'
    cost blowup)."""
    cat = re.sub(r"[^A-Za-z0-9_]", "", str(category or ""))                       # -> OST_* identifier
    names = [re.sub(r'[^\w .\-/%()]', "", str(n))[:64] for n in (param_names or []) if str(n).strip()]
    names_cs = ", ".join('"%s"' % n for n in names) or '"Mark"'
    scope = "document, document.ActiveView.Id" if in_view_only else "document"
    return (
        f"var names = new string[] {{ {names_cs} }};\n"
        f'var bic = (BuiltInCategory)System.Enum.Parse(typeof(BuiltInCategory), "{cat}");\n'
        f"var col = new FilteredElementCollector({scope}).OfCategory(bic).WhereElementIsNotElementType();\n"
        "return col.Select(e => new {\n"
        "    id = e.Id.Value,\n"
        "    name = e.Name,\n"
        "    parameters = names.ToDictionary(n => n, n => {\n"
        "        var p = e.LookupParameter(n);\n"
        "        return p == null ? null : (p.StorageType == StorageType.String ? p.AsString() : p.AsValueString());\n"
        "    })\n"
        "}).ToList();"
    )


GET_PARAMS_BULK_TOOL: dict = {
    "name": "get_parameters_bulk",
    "description": (
        "Read one or more parameter values for EVERY element of a category in ONE call. Returns a list of "
        "{id, name, parameters}. ALWAYS use this to inspect/audit many elements (e.g. the Mark of all "
        "doors) — never call get_element_parameters per element. Read-only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Revit BuiltInCategory, e.g. 'OST_Doors', 'OST_Walls', 'OST_Windows'"},
            "parameter_names": {"type": "array", "items": {"type": "string"},
                                "description": "Instance parameter names to read, e.g. ['Mark']"},
            "in_view_only": {"type": "boolean", "description": "Limit to the active view (default false = whole model)"},
        },
        "required": ["category", "parameter_names"],
    },
}

# The full toolset the executor sees = curated ergonomic tools + the entire plugin surface
# (+ the read-only warnings reader + the gated Revit API fallback). send_code_to_revit is never exposed
# by name; both get_warnings and the fallback are the only sanctioned paths to it (get_warnings is a
# fixed read), so they ride the same EXECUTOR_ALLOW_API_FALLBACK toggle.
TOOL_SCHEMAS: list[dict] = (
    CURATED_SCHEMAS + revit_tools.TOOL_SCHEMAS
    + ([GET_WARNINGS_TOOL, GET_PARAMS_BULK_TOOL, EXECUTE_API_TOOL] if API_FALLBACK_ENABLED else [])
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

READ IN BULK, NEVER IN A LOOP — this is a hard cost rule:
To inspect or audit MANY elements (e.g. "check the Mark of every door", "fix all the numbering"), call
get_parameters_bulk ONCE with the category + parameter_names (e.g. category 'OST_Doors', ['Mark']). It
returns the whole list of {id, name, parameters} in a single call. NEVER call get_element_parameters /
get_element_info element-by-element in a loop — reading 60 doors one at a time costs ~100x more and will
be cut off after a few calls.

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
- "No tag family found": tag_element now resolves the tag type automatically, so this is rare; if it
  still happens, call get_available_family_types on the TAG category (e.g. 'OST_DoorTags',
  'OST_WindowTags') and retry tag_element with tag_type_id set to that FamilyTypeId. Do NOT loop the
  tag — one successful tag is enough; do not re-tag an element that already returned a tag id.
- HOSTED FAMILIES (doors & windows): place_element places these correctly — it resolves the family
  to its loaded type and hosts it on a wall — BUT it needs a host wall. So for a door/window, pass
  host_wall_id: get it from get_selected_elements (the user's selected wall) or pick_point ON a wall,
  then call place_element with that host_wall_id and a point on the wall. If place_element still
  returns "created 0" AFTER you have given a valid host_wall_id and confirmed the family is loaded
  (get_available_family_types), only THEN treat it as a capability gap and use execute_revit_api
  (document.Create.NewFamilyInstance(point, symbol, hostWall, level, NonStructural)).
- A structured tool returning an error is EXPECTED and recoverable — stay on the structured tools and
  fix the call. Do NOT respond to a normal place/set/tag/create failure by switching to the Revit API
  fallback; that tool is only for operations no structured tool supports (see below).
- READ REVIT'S WARNINGS: a tool can report success while Revit is still holding a WARNING against the
  element (duplicate Mark, unhosted, overlap). After a write — especially place_element / set_parameter
  on a Mark — call get_warnings to check the element you just touched. Warnings are SIGNALS, not proof
  of failure: if one names your element (duplicate Mark -> set a unique Mark and re-verify; unhosted ->
  re-host), fix it; if it's acceptable for the goal, say so explicitly. Never silently ignore an
  Error-severity warning on the element you just created. get_warnings also surfaces recent dialog
  pop-ups — if Revit raised one, read it and act on it.
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

# The plugin's create_point_based_element resolves the family to place from a FamilyTypeId (or a
# category) — its PointElement model has NO family-name field, so sending a name silently resolves
# nothing and it "succeeds" creating 0 elements. So place_element must resolve the family NAME to a
# loaded FamilyTypeId first. These are the categories we search when no explicit category is given.
_PLACEABLE_CATEGORIES = [
    "OST_Doors", "OST_Windows", "OST_Furniture", "OST_Casework", "OST_GenericModel",
    "OST_PlumbingFixtures", "OST_LightingFixtures", "OST_ElectricalFixtures",
    "OST_SpecialtyEquipment", "OST_FurnitureSystems", "OST_MechanicalEquipment",
    "OST_Planting", "OST_Entourage", "OST_Columns", "OST_StructuralColumns",
]


def _resolve_type_id(family_name: str, type_name: str | None = None,
                     category: str | None = None) -> tuple:
    """Resolve a family (+optional type) to its loaded FamilyTypeId. Returns (type_id|None, category).
    Needed because create_point_based_element matches by typeId/category, NOT by family name."""
    from mcp_server import revit_bridge as rb
    cats = [category] if category else _PLACEABLE_CATEGORIES
    res = rb._call_plugin("get_available_family_types", {"categoryList": cats})
    rows = res if isinstance(res, list) else []
    fam = (family_name or "").strip().lower()
    matches = [r for r in rows if (r.get("FamilyName") or "").strip().lower() == fam]
    if not matches:
        return None, category
    tn = (type_name or "").strip().lower()
    chosen = (next((r for r in matches if (r.get("TypeName") or "").strip().lower() == tn), None)
              if tn else None) or matches[0]
    try:
        return int(chosen.get("FamilyTypeId")), category
    except (TypeError, ValueError):
        return None, category


def _family_match(family_name: str, category: str | None) -> dict:
    """For a routine whose family isn't loaded in THIS model: the closest loaded family in the category.
    {best: row|None, score, available: [family names]}. Score = shared lowercased tokens (0 = unrelated),
    so the caller can auto-substitute a real match vs. listing options/asking when nothing is close."""
    from mcp_server import revit_bridge as rb
    cats = [category] if category else _PLACEABLE_CATEGORIES
    res = rb._call_plugin("get_available_family_types", {"categoryList": cats})
    rows = res if isinstance(res, list) else []
    want = set(re.findall(r"[a-z0-9]+", (family_name or "").lower()))

    def score(r):
        fam = set(re.findall(r"[a-z0-9]+", (r.get("FamilyName") or "").lower()))
        typ = set(re.findall(r"[a-z0-9]+", (r.get("TypeName") or "").lower()))
        return len(want & fam) + 0.5 * len(want & typ)

    best = max(rows, key=score) if rows else None
    return {"best": best, "score": (score(best) if best else 0),
            "available": sorted({(r.get("FamilyName") or "") for r in rows})[:12]}


# Element category -> its TAG category. The plugin's tag_element auto-find is broken (it compares the
# tag family's category to the ELEMENT's category, which never match — a door tag is OST_DoorTags, the
# door is OST_Doors), so we resolve the tag type id ourselves and pass it, like place_element's typeId.
_TAG_CATEGORY = {
    "Doors": "OST_DoorTags", "Windows": "OST_WindowTags", "Walls": "OST_WallTags",
    "Rooms": "OST_RoomTags", "Floors": "OST_FloorTags", "Furniture": "OST_FurnitureTags",
    "Casework": "OST_CaseworkTags", "Generic Models": "OST_GenericModelTags",
    "Lighting Fixtures": "OST_LightingFixtureTags", "Plumbing Fixtures": "OST_PlumbingFixtureTags",
    "Structural Columns": "OST_StructuralColumnTags", "Structural Framing": "OST_StructuralFramingTags",
}


def _tag_category_for(category: str | None) -> str | None:
    if not category:
        return None
    if category in _TAG_CATEGORY:
        return _TAG_CATEGORY[category]
    base = category[:-1] if category.endswith("s") else category   # Doors->Door->OST_DoorTags
    return "OST_" + base.replace(" ", "") + "Tags"


def _resolve_tag_type_id(element_id: int) -> int | None:
    """Resolve the tag FamilyTypeId for an element by querying its category and the matching loaded
    tag family — so tag_element can pass tagTypeId instead of relying on the plugin's broken auto-find."""
    from mcp_server import revit_bridge as rb
    info = rb._call_plugin("get_element_info", {"elementId": int(element_id)})
    resp = info.get("Response") if isinstance(info, dict) else None
    category = resp.get("category") if isinstance(resp, dict) else None
    tag_cat = _tag_category_for(category)
    if not tag_cat:
        return None
    rows = rb._call_plugin("get_available_family_types", {"categoryList": [tag_cat]})
    for r in (rows if isinstance(rows, list) else []):
        tid = r.get("FamilyTypeId")
        if tid:
            try:
                return int(tid)
            except (TypeError, ValueError):
                pass
    return None


def _ok(res: Any) -> bool:
    return isinstance(res, dict) and bool(res.get("Success"))


def _msg(res: Any) -> str:
    if isinstance(res, dict):
        return res.get("Message") or res.get("error") or ""
    return str(res)


def _warnings_from(res) -> dict | None:
    """Extract {warnings, dialogs} from a get_warnings command response, or None if the command isn't
    available (so the caller falls back to the read-only snippet). Tolerates the plugin's various
    envelope shapes (top-level, Response/response, result-as-JSON-string)."""
    if not isinstance(res, dict) or res.get("error"):
        return None
    body = res.get("Response") or res.get("response") or res.get("result") or res
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return None
    if isinstance(body, dict) and "warnings" in body:
        w, d = body.get("warnings"), body.get("dialogs")
        return {"warnings": w if isinstance(w, list) else [],
                "dialogs": d if isinstance(d, list) else []}
    return None


def real_dispatch(name: str, args: dict) -> dict:
    """Execute one tool against the live Revit plugin. Returns a normalized result."""
    from mcp_server import revit_bridge as rb

    if name == "place_element":
        loc = args.get("location", {})
        # Resolve the family NAME -> a loaded FamilyTypeId (the plugin matches by typeId/category, not
        # by name — sending a name created 0 elements every time). This is what makes placement work.
        type_id, cat = _resolve_type_id(args.get("family_name"), args.get("type_name"),
                                        args.get("category"))
        substitute = None
        if type_id is None:
            # The routine's family isn't loaded in THIS model (routines are learned per-model). Map to the
            # CLOSEST loaded family instead of placing a blind default (which silently rolls back) or
            # thrashing through retries. If nothing is close, list the options / ask — don't guess.
            m = _family_match(args.get("family_name"), cat or args.get("category"))
            if m["best"] and m["best"].get("FamilyTypeId") is not None and m["score"] > 0:
                type_id = int(m["best"]["FamilyTypeId"])
                cat = m["best"].get("Category") or cat or args.get("category")
                substitute = m["best"].get("FamilyName")
            else:
                avail = m["available"]
                return {"success": False, "message":
                        f"family '{args.get('family_name')}' is not loaded in this model" +
                        (f"; loaded families here are {avail} — place one of those (place_element with that "
                         "family_name) or ask the user to load the family." if avail
                         else "; no placeable families in this category — ask the user to load the family.")}
        data: dict = {
            "locationPoint": {"x": loc.get("x", 0), "y": loc.get("y", 0), "z": loc.get("z", 0)},
            "baseLevel": 0, "baseOffset": 0, "typeId": type_id,
        }
        if cat:
            data["category"] = cat
        if args.get("host_wall_id"):
            data["hostWallId"] = int(args["host_wall_id"])   # the handler hosts doors/windows on it
        res = rb._call_plugin("create_point_based_element", {"data": [data]})
        eid = rb._extract_element_id(res) if isinstance(res, dict) else None
        if _ok(res) and eid:
            # VERIFY the element actually persisted: create_point_based_element can report success on a
            # create that ROLLED BACK (a door/window with no valid host wall). That false "placed" is what
            # sent the agent thrashing — read it back, and only THEN report success.
            info = rb._call_plugin("get_element_info", {"elementId": int(eid)})
            gone = isinstance(info, dict) and (info.get("Success") is False or "not found" in
                   str(info.get("Message") or info.get("message") or info.get("error") or "").lower())
            if gone:   # only a DEFINITIVE not-found blocks success; an empty/transient reply is trusted
                return {"success": False, "message":
                        "the placement did not persist (the element no longer exists). For a door/window "
                        "this usually means there was no host wall — pass host_wall_id from a wall at this "
                        "point (get_selected_elements, or pick_point on a wall)."}
            msg = ("placed" if not substitute else
                   f"placed using the closest loaded family '{substitute}' — the routine's "
                   f"'{args.get('family_name')}' is not loaded in this model")
            out = {"success": True, "message": msg, "element_id": eid}
            if substitute:
                out["substituted_family"] = substitute
            return out
        return {"success": False,
                "message": _msg(res) or "no element created (check the family is loaded and, for a "
                                        "door/window, that host_wall_id is a wall at this point)"}

    if name == "set_parameter":
        res = rb._call_plugin("set_element_parameter", {
            "elementId": int(args["element_id"]),
            "parameters": [{"name": args["name"], "value": args["value"]}],
        })
        return {"success": _ok(res), "message": _msg(res) or "parameter set"}

    if name == "tag_element":
        eid = int(args["element_id"])
        # Resolve the tag type id (the plugin's auto-find by category is broken — see _resolve_tag_type_id)
        # and pass it as tagTypeId so the plugin uses it directly instead of failing "no tag family found".
        tag_type_id = args.get("tag_type_id") or _resolve_tag_type_id(eid)
        # Honor a requested offset (mm) so "place the tag away from the door for readability" actually
        # moves the tag. Default offsetY=500 (clear of the element), matching the plugin's own default —
        # the old hardcoded 0,0 silently overrode it, making the preference inert.
        params = {"elementId": eid, "useLeader": False,
                  "offsetX": float(args.get("offset_x", 0.0)),
                  "offsetY": float(args.get("offset_y", 500.0))}
        if tag_type_id:
            params["tagTypeId"] = str(int(tag_type_id))
        res = rb._call_plugin("tag_element", params)
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

    # Read-only introspection: Revit's live warnings/errors + recent dialog pop-ups. Prefer the
    # DEDICATED plugin command (reliable external-event path, post-rebuild); fall back to the fixed
    # send_code_to_revit snippet if that command isn't deployed yet. Either way the model never authors
    # code and nothing is written — so the loop + verifier can react to what Revit is flagging.
    if name == "get_warnings":
        try:
            parsed = _warnings_from(rb._call_plugin("get_warnings", {}))
        except Exception:
            parsed = None
        if parsed is None:                              # fallback: fixed read-only snippet
            try:
                snip = rb._call_plugin("send_code_to_revit",
                                       {"code": GET_WARNINGS_CODE, "transactionMode": "none"}, timeout=8)
            except Exception:
                snip = None
            if isinstance(snip, dict) and not snip.get("error") and snip.get("success", True):
                raw = snip.get("result")
                try:
                    w = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, TypeError):
                    w = None
                if isinstance(w, list):
                    parsed = {"warnings": w, "dialogs": []}
        if parsed is None:
            return {"success": False, "warnings": [], "dialogs": [],
                    "message": "could not read warnings (dedicated command not deployed; API path unavailable)"}
        return {"success": True, "warnings": parsed["warnings"], "dialogs": parsed["dialogs"],
                "message": f"{len(parsed['warnings'])} warning(s), {len(parsed['dialogs'])} dialog(s)"}

    # Batch read: every element of a category + the requested params in ONE call (fixed, audited snippet).
    if name == "get_parameters_bulk":
        code = _bulk_params_code(args.get("category", ""), args.get("parameter_names") or [],
                                 bool(args.get("in_view_only")))
        try:
            res = rb._call_plugin("send_code_to_revit", {"code": code, "transactionMode": "none"}, timeout=60)
        except Exception as exc:
            return {"success": False, "message": f"bulk read failed: {exc}"}
        if isinstance(res, dict) and res.get("error"):
            return {"success": False, "message": str(res["error"])}
        raw = res.get("result") if isinstance(res, dict) else res
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            items = raw
        n = len(items) if isinstance(items, list) else 0
        return {"success": True, "message": f"read {n} element(s) in one call", "elements": items}

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
            if ok:
                _record_capability_gap(code, args)   # raw material for a future compiled tool
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


def _parse_token(v) -> tuple | None:
    """Split a value into (prefix, number, zero-pad width, suffix), or None if it has no number.
    'D-101' -> ('D-', 101, 3, ''); 'W-09' -> ('W-', 9, 2, '')."""
    m = re.search(r"(\d+)(\D*)$", str(v))
    if not m:
        return None
    return str(v)[:m.start(1)], int(m.group(1)), len(m.group(1)), m.group(2)


def induce_sequence_rule(values: list) -> dict | None:
    """UNDERSTAND the sequence: infer its generating rule (shared prefix/suffix/zero-pad + a constant
    numeric STEP) from the observed values, instead of assuming a hardcoded +1. Returns
    {prefix, suffix, pad, step, last} or None if the values aren't a single consistent arithmetic
    sequence. e.g. ['W-100','W-105','W-110'] -> step 5 (next 'W-115'), not 'W-111'."""
    parsed = [p for p in (_parse_token(v) for v in values if v not in (None, "")) if p]
    if len(parsed) < 3:
        return None            # need >=3 points so >=2 agreeing diffs confirm the step (avoid guessing
                               # a step from 2 ambiguous points — that's over-generalization)
    if len({p[0] for p in parsed}) != 1 or len({p[3] for p in parsed}) != 1:
        return None                                  # mixed prefix/suffix -> not one sequence
    nums = sorted({p[1] for p in parsed})
    if len(nums) < 3:
        return None                                  # <3 distinct numbers -> step unconfirmed
    diffs = {b - a for a, b in zip(nums, nums[1:])}
    if len(diffs) != 1 or 0 in diffs:
        return None                                  # not a constant non-zero arithmetic step
    return {"prefix": parsed[0][0], "suffix": parsed[0][3],
            "pad": max(p[2] for p in parsed), "step": diffs.pop(), "last": nums[-1]}


def next_from_rule(rule: dict, used=None) -> str:
    """The next value per an induced sequence rule, skipping any value already in `used` (uniqueness)."""
    used = {str(u) for u in (used or set())}
    n = rule["last"] + rule["step"]
    for _ in range(10000):
        cand = f"{rule['prefix']}{str(n).zfill(rule['pad'])}{rule['suffix']}"
        if cand not in used:
            return cand
        n += rule["step"]
    return f"{rule['prefix']}{str(n).zfill(rule['pad'])}{rule['suffix']}"


def _example_contexts(examples: list, pn: str) -> list:
    """For variable param `pn`, build [{value, context}] from the examples — context = the sibling
    param values + level observed in the SAME example — so rule_induction.induce_rule can infer a
    conditional or a per-context sequence (e.g. Mark per level)."""
    def _c(v):
        return None if v is None or str(v).strip().lower() in ("", "none", "null") else v
    rows = []
    for e in (examples if isinstance(examples, list) else []):
        if not isinstance(e, dict):
            continue                                       # tolerate corrupted/hand-edited records
        val, ctx = None, {}
        for a in (e.get("actions") or []):
            if not isinstance(a, dict):
                continue
            an, av = a.get("param_name"), _c(a.get("param_value_after") or a.get("param_value"))
            if an == pn:
                if av is not None:
                    val = av
            elif an and av is not None:
                ctx[an] = av
            if a.get("level_name"):
                ctx.setdefault("level", a.get("level_name"))
        if val is not None:
            rows.append({"value": val, "context": ctx})
    return rows


def resolve_routine_values(motif: dict, examples: list | None = None,
                           last_values: dict | None = None,
                           existing_values: dict | None = None,
                           context: dict | None = None) -> dict:
    """Decide the value to USE for each parameter the routine sets. A constant value stays; a
    variable one (e.g. Mark) becomes the NEXT in its observed sequence — incremented from the value
    we last set (project memory), or from the highest value seen in the recorded examples.

    `existing_values` = {param_name: set(values already in the LIVE model)}: a computed variable value
    is advanced past any value already in use, so we never silently assign a DUPLICATE Mark (Revit
    allows duplicate marks but flags them as a warning — and a BIM reviewer will catch it)."""
    from .rule_induction import induce_rule, apply_rule    # lazy import — avoids a circular import
    examples = examples or []
    last_values = last_values or {}
    existing = existing_values or {}

    def _clean(v):
        return None if v is None or str(v).strip().lower() in ("", "none", "null") else v

    steps = motif.get("steps", []) if isinstance(motif, dict) else []
    # Context for conditional / per-context rules: the routine's CONSTANT params + any live context
    # passed in (e.g. the active level), augmented per-param with values resolved earlier in this pass.
    base_ctx = {s.get("param_name"): _clean(s.get("param_value")) for s in steps
                if s.get("param_name") and (s.get("param_value_type") or "").lower() != "variable"
                and _clean(s.get("param_value")) is not None}
    base_ctx.update({k: v for k, v in (context or {}).items() if v is not None})

    out: dict = {}
    for s in steps:
        pn = s.get("param_name")
        if not pn:
            continue
        ptype = (s.get("param_value_type") or "").lower()
        pv = _clean(s.get("param_value"))
        if pv is not None and ptype != "variable":
            out[pn] = pv                                # constant — use as recorded
            continue
        # CONTEXTUAL UNDERSTANDING: a conditional (value chosen by a condition on a sibling/level) or a
        # per-context sequence (e.g. Mark numbered per level), induced from the examples' context and
        # applied with the live context. Used only when a rule is found AND it determines a value.
        crule = induce_rule(_example_contexts(examples, pn))
        if crule:
            cval = apply_rule(crule, {**base_ctx, **out}, existing.get(pn))
            if cval is not None:
                out[pn] = cval
                continue
        # UNDERSTAND THE SEQUENCE: gather every observed value for this param (the value we set last
        # + all recorded examples) and INDUCE its generating rule (prefix/step/zero-pad), so the next
        # value follows the user's actual pattern (e.g. step 5, or per-scheme zero-pad) rather than a
        # hardcoded +1. Fall back to the simple increment only when no consistent rule can be learned.
        observed = [v for v in (
            [_clean(last_values.get(pn))]
            + [_clean(a.get("param_value_after") or a.get("param_value"))
               for e in examples for a in (e.get("actions") or []) if a.get("param_name") == pn])
            if v is not None]
        # ANCHOR TO THE LIVE MODEL: the elements already in the project are far stronger evidence of the
        # user's real naming CONVENTION than the routine's few recorded examples. If the model already
        # uses a scheme (e.g. doors 'TU 29'…'TU 233'), continue IT ('TU 234') instead of replaying the
        # routine's private counter ('106'). This also closes the correction loop for free: when the user
        # corrects a Mark, that value becomes a real element, so the next resolve reads it and continues.
        live = [v for v in (_clean(x) for x in (existing.get(pn) or ())) if v is not None]
        if live:
            # The model's own elements WIN over the routine's examples — even if the examples form a clean
            # numeric rule. Use a rule induced from the live values if one exists (e.g. constant step);
            # otherwise (real schemes have gaps, so induce returns None) continue from the highest mark in
            # the live scheme ('TU 233' -> 'TU 234'). This is what stops a corrected/real convention from
            # being clobbered by the routine's private counter ('106').
            lr = induce_sequence_rule(live)
            if lr:
                out[pn] = next_from_rule(lr, existing.get(pn))
                continue
            nxt = next_in_sequence(_max_in_sequence(live))
            if nxt is not None:
                used = {str(v) for v in (existing.get(pn) or set())}
                guard = 0
                while str(nxt) in used and guard < 10000:
                    adv = next_in_sequence(nxt)
                    if adv is None or adv == nxt:
                        break
                    nxt, guard = adv, guard + 1
                out[pn] = nxt
                continue
        # no usable live values: induce the scheme from the routine's own examples + last_values.
        rule = induce_sequence_rule(observed)
        if rule:
            out[pn] = next_from_rule(rule, existing.get(pn))
            continue
        # fallback (single sample / irregular, model empty): the simple next-in-sequence.
        nxt = next_in_sequence(_clean(last_values.get(pn)))
        if nxt is None:
            seen = [_clean(a.get("param_value_after") or a.get("param_value"))
                    for e in examples for a in (e.get("actions") or [])
                    if a.get("param_name") == pn]
            nxt = next_in_sequence(_max_in_sequence([v for v in seen if v is not None]))
        if nxt is not None:
            used = {str(v) for v in (existing.get(pn) or set())}
            guard = 0
            while str(nxt) in used and guard < 1000:    # advance past values already in the model
                adv = next_in_sequence(nxt)
                if adv is None or adv == nxt:
                    break                               # cannot advance further — best effort
                nxt, guard = adv, guard + 1
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
    prior_messages: list | None = None,
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
    # Persistent session: continue from prior_messages (the running tool-use history of earlier tasks)
    # so the agent REMEMBERS what it already did — element ids it created, families it found loaded,
    # what is already tagged — instead of re-grounding every task. prior_messages must end on an
    # assistant turn (a cleanly-finished prior run) for the new user turn to be valid.
    _new_turn = {"role": "user", "content": goal}
    messages: list[dict] = (list(prior_messages) + [_new_turn]) if prior_messages else [_new_turn]
    tool_calls: list[dict] = []
    attempts = 0
    read_loop_count = 0        # per-element reads this run — capped so the model can't snowball cost by
                               # querying elements one at a time (a single batched read costs ~nothing)
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
                    "nudged": completion_nudges, "escalated": escalated, "messages": messages}

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
                    "nudged": completion_nudges, "escalated": escalated, "messages": messages}

        results_content = []
        for tu in tool_uses:
            name, args, tid = tu.name, (tu.input or {}), tu.id
            attempts += 1
            if name not in ALLOWED_TOOLS:
                result = {"success": False, "message": f"tool '{name}' is not allowed"}
            else:
                emit("tool", {"name": name, "args": args})
                if name in _READ_LOOP_TOOLS:
                    read_loop_count += 1
                if name in _READ_LOOP_TOOLS and read_loop_count > _READ_LOOP_CAP:
                    # Hard cost guard: the model is reading elements ONE AT A TIME (the $2 "audit 60 doors"
                    # failure mode — each call snowballs the conversation). Force a single batched read.
                    result = {"success": False, "message":
                              f"Stopped: you've read {read_loop_count} elements one-by-one. Do NOT loop "
                              "get_element_parameters/get_element_info per element — it is very expensive. "
                              "Use ONE execute_revit_api snippet with a FilteredElementCollector over the "
                              "category to read them ALL at once, then work from that single result."}
                elif (guard_api_fallback and name == "execute_revit_api" and not api_reaffirmed
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


def _render_repeat(rep: dict) -> str:
    """Render a step's loop spec as an imperative 'for each ...' clause for the goal text."""
    if rep.get("over"):
        parts = [f"For EACH {rep['over']}"]
    elif rep.get("count"):
        parts = [f"Repeat {rep['count']} times"]
    else:
        parts = ["For each item"]
    if rep.get("spacing_mm"):
        parts.append(f"spaced {rep['spacing_mm']} mm apart along it")
    if rep.get("mark_expr"):
        parts.append(f"setting {rep.get('index_param', 'Mark')} = {rep['mark_expr']} (i = 1, 2, 3, ...)")
    return ", ".join(parts) + ":"


def build_goal(motif: dict, location: dict | None = None, param_values: dict | None = None) -> str:
    """Turn a detected routine (motif + steps) into the executor's goal prompt. Reads the Pattern
    Agent's real step fields (action_type, family_name, tag_family_name, param_name/value) AND the
    richer-workflow extensions (element_role/host_role, condition, value_expr, repeat, plus
    motif.elements for multi-element compounds) — flat motifs render exactly as before.
    `param_values` (from resolve_routine_values) supplies the concrete value for each parameter."""
    steps = motif.get("steps", []) if isinstance(motif, dict) else []
    pvals = param_values or {}
    lines = [f"Routine: {motif.get('name', 'Detected routine')}", "Steps to reproduce IN ORDER:"]

    # Compound preamble: name the distinct elements + their host relationships up front, so later
    # steps can refer to each by role instead of the ambiguous "the placed element".
    elements = motif.get("elements") or []
    if elements:
        lines.append("This routine creates SEVERAL related elements — track each by its role:")
        for el in elements:
            host = el.get("host")
            lines.append(f"  - '{el.get('role', '?')}': family '{el.get('family') or el.get('family_name') or '?'}'"
                         + (f", hosted on the '{host}'." if host else "."))
        lines.append("Steps:")

    for i, s in enumerate(steps, 1):
        a = (s.get("action_type") or s.get("action") or "").strip()
        al = a.lower()
        fam = s.get("family_name") or s.get("family_type") or ""
        pn = s.get("param_name") or ""
        pv = pvals.get(pn, s.get("param_value")) if pn else None
        tagfam = s.get("tag_family_name") or ""
        role = s.get("element_role") or ""
        host_role = s.get("host_role") or ""
        value_expr = s.get("value_expr") or ""
        condition = s.get("condition") or ""
        repeat = s.get("repeat") or None
        target = f"the '{role}'" if role else "the placed element"

        if ("place" in al or "create" in al) and fam:
            base = f"Place the family '{fam}'"
            if role:
                base += f" (call it the '{role}')"
            if host_role:
                base += f", hosted on the '{host_role}' created in this routine"
            base += "."
        elif "tag" in al:
            base = f"Tag {target}" + (f" with '{tagfam}'." if tagfam else ".")
        elif pn:
            if value_expr:
                base = (f"Set parameter '{pn}' on {target} to the COMPUTED value: {value_expr} "
                        "(evaluate it against the live model).")
            elif pv in (None, ""):
                base = (f"Set parameter '{pn}' on {target} "
                        "(value is provided at runtime — ask the user if you don't have it).")
            else:
                base = (f"Set parameter '{pn}' = '{pv}' on {target} "
                        "(this is the next value in the sequence — use it as-is).")
        elif a:
            base = f"{a}."
        else:
            continue

        if condition:
            base = f"ONLY IF {condition} — {base}"
        if repeat:
            base = _render_repeat(repeat) + " " + base
        lines.append(f"  {i}. {base}")

    if location:
        lines.append(f"Place it at approximately x={location.get('x')}, y={location.get('y')} mm. "
                     "If it needs a host wall and none is there, ask the user to pick a point on a wall.")
    else:
        lines.append("No location given yet — if the user has a wall selected, host on it; otherwise use "
                     "pick_point to have the user click where to place it (on a wall for a door/window).")
    lines.append("Do EVERY step in order — place, set each parameter, and tag — self-correcting on any "
                 "tool error. Do not stop after placing; the routine is only done when all steps succeeded.")
    if elements or any((s.get("repeat") or s.get("condition")) for s in steps):
        lines.append("This is a richer routine: honour each step's LOOP (repeat for every item, advancing "
                     "the index) and CONDITION (act only when the guard holds), and keep each element's "
                     "role straight when you set parameters and tag.")
    return "\n".join(lines)


def verify_outcome(param_values: dict, placed_id, dispatch_fn: Callable[[str, dict], dict] = real_dispatch) -> dict:
    """Deterministic post-condition check: read the placed element's parameters BACK from the live
    model and confirm each intended value actually stuck (a 'committed' tool result is not proof the
    value is right). Returns {ok, issues, actual}. Best-effort + cheap (one read); a query/no-op run
    (no placed_id or no params) verifies vacuously."""
    pv = param_values or {}
    names = [n for n in pv]
    if placed_id is None or not names:
        return {"ok": True, "issues": [], "actual": {}}
    try:
        res = dispatch_fn("get_element_parameters", {"elementId": int(placed_id), "parameterNames": names})
    except Exception as exc:
        return {"ok": True, "issues": [], "actual": {}, "skipped": str(exc)}   # don't fail on a read error
    rows = []
    if isinstance(res, dict):
        rows = res.get("response") or res.get("Response") or []
    actual: dict = {}
    for row in (rows if isinstance(rows, list) else []):
        if isinstance(row, dict) and row.get("name") is not None:
            actual[row["name"]] = "" if row.get("value") is None else str(row["value"])
    issues = [f"'{n}' reads back {actual.get(n)!r}, expected {str(v)!r}"
              for n, v in pv.items() if n in actual and actual[n] != str(v)]
    # Surface Revit's OWN warnings on the placed element (advisory). A warning is not proof the value is
    # wrong, so it doesn't flip ok by itself — but an Error-severity warning naming THIS element is a
    # genuine problem and is escalated to an issue.
    warnings: list = []
    try:
        w = dispatch_fn("get_warnings", {})
        if isinstance(w, dict) and w.get("success"):
            pid = str(placed_id)
            for warn in (w.get("warnings") or []):
                ids = {str(i) for i in (warn.get("failing_ids") or []) + (warn.get("additional_ids") or [])}
                if pid in ids:
                    warnings.append(warn)
                    if str(warn.get("severity", "")).lower() == "error":
                        issues.append(f"Revit error on this element: {warn.get('description')}")
    except Exception:
        pass
    return {"ok": not issues, "issues": issues, "actual": actual, "warnings": warnings}


def build_freeform_goal(task: str, context: str = "") -> str:
    """Wrap a free-form natural-language request as an executor goal — the conversational path that
    handles ARBITRARY tasks and model questions, not just learned routines. The executor already has
    the full read/query/create tool surface, so this just frames the request + the ground-first and
    do-only-what-was-asked discipline. `context` carries the recent conversation + work already done
    so consecutive related tasks don't re-do completed steps."""
    ctx = ("RECENT CONVERSATION & WORK ALREADY DONE (build on this — do NOT redo steps already "
           f"completed):\n{context.strip()}\n\n" if context and context.strip() else "")
    return (
        ctx +
        "The user asked you to do the following in their LIVE Revit model:\n\n"
        f"    {task.strip()}\n\n"
        "Carry it out with the tools. GROUND yourself first with the read/query tools "
        "(get_active_view, inspect_model, get_selected_elements, get_available_family_types, "
        "ai_element_filter, get_element_info/parameters) before changing anything. If this is purely a "
        "QUESTION about the model, answer it from the query tools and DO NOT modify anything. If it "
        "needs changes, do them step by step, self-correcting on errors, and touch ONLY what was "
        "asked — never unrelated elements. When finished, reply with a short plain-text summary of "
        "what you did or what you found."
    )


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
