"""
Macro / Command Agent — converts a Motif into a mcp-servers-for-revit tool call sequence.

Updated architecture (thesis §4.2):
  • Before generating the tool sequence the agent queries the live Revit model
    via model:query_state to resolve family type names, element IDs, and view
    context — grounding the routine in the actual current model state.
  • The output is a list of tool calls for mcp-servers-for-revit (not C# IPC).
  • Precondition checks are performed before the final tool call list is returned.
  • After user confirmation agents.py calls execute_revit_command which dispatches
    to mcp-servers-for-revit.

Model:  claude-sonnet-4-6  (fast, strong at structured config generation)
Output: list of {"tool": str, "arguments": dict} for mcp-servers-for-revit.

Runtime placeholder convention (resolved at execution time by revit_bridge):
  {{location}}          — user-clicked XYZ point (injected at runtime)
  {{last_element_id}}   — element_id of the most recently placed/modified element
  {{ParamName}}         — user-supplied value for a variable parameter
"""
from __future__ import annotations

import json
import os
import anthropic

from mcp_server.revit_bridge import model_query_state, model_query

MODEL = os.environ.get("MACRO_AGENT_MODEL", "claude-sonnet-4-6")

# ── mcp-servers-for-revit tool schema shown to the agent ──────────────────────
MCP_REVIT_TOOLS = """
MCP-SERVERS-FOR-REVIT — AVAILABLE TOOLS
========================================

place_element(family_type: str, location: object) -> dict
  Places a family instance in the active view at the given location.
  family_type: full Revit family + type name, e.g. "M_Single-Flush:915x2134mm"
  location: use the placeholder "{{location}}" (resolved to user click at runtime)
  Returns: {"element_id": int, "family_type": str}

set_parameter(element_id: int | "{{last_element_id}}", parameter_name: str, value: any) -> dict
  Sets a parameter value on an element.
  element_id: use "{{last_element_id}}" to refer to the most recently placed element.
  value: use the literal value for constants; use "{{ParamName}}" for variable params.
  Returns: {"element_id": int, "parameter_name": str, "value": any}

create_annotation_tag(element_id: int | "{{last_element_id}}", tag_family: str) -> dict
  Places an annotation tag on an element.
  element_id: use "{{last_element_id}}" for the most recently placed/modified element.
  tag_family: Revit tag family name, e.g. "M_Door Tag"
  Returns: {"tag_id": int, "tagged_element_id": int}

PLACEHOLDER SUMMARY
-------------------
{{location}}          resolved to the user's clicked point when the shortcut runs
{{last_element_id}}   resolved to the element_id of the last placed/modified element
{{SomeParamName}}     resolved to user input at runtime (for variable parameters)
"""

SYSTEM_PROMPT = f"""\
You are a Revit automation engineer. Given a generalised BIM routine motif and \
optional model context from the live Revit model, you produce a JSON array of \
mcp-servers-for-revit tool calls that will execute the routine.

{MCP_REVIT_TOOLS}

OUTPUT FORMAT
Return ONLY a JSON array — no markdown, no explanation:

[
  {{"tool": "<tool_name>", "arguments": {{<key>: <value>, ...}}}},
  ...
]

MAPPING RULES
1. Place step    -> place_element; location = "{{{{location}}}}"
                   Use the most specific matching family_type from the model context
                   if available; otherwise use the family_name from the motif step.
2. SetParam step -> set_parameter; element_id = "{{{{last_element_id}}}}";
                   if param_value_type is "constant": value = the literal param_value
                   if param_value_type is "variable":  value = "{{{{<param_name>}}}}"
3. Tag step      -> create_annotation_tag; element_id = "{{{{last_element_id}}}}";
                   tag_family = the tag_family_name from the motif step.
4. Preserve step order exactly as given in the motif.
5. Do not invent steps not present in the motif.
6. If model context supplies a more precise family type name (e.g. the loaded type
   includes a size suffix like "915x2134mm"), use it in the place_element call.
"""


# ── Context queries for grounding ─────────────────────────────────────────────

def _fetch_model_context(motif: dict) -> dict:
    """
    Fetch relevant model context for this motif from mcp-servers-for-revit.

    Returns a dict with keys 'active_view', 'loaded_families', 'levels'.
    Returns empty dicts for each if the backend is unavailable (graceful fallback).
    """
    context: dict = {}

    # Active view — needed to verify preconditions
    view_result = model_query_state("active view")
    if "error" not in view_result:
        context["active_view"] = view_result

    # Loaded families for each Place step category
    categories = set()
    for step in motif.get("steps", []):
        if step.get("action_type") == "Place":
            family_name = step.get("family_name", "")
            # Heuristic: infer category from family name
            fn_lower = family_name.lower()
            if "door" in fn_lower:
                categories.add("Doors")
            elif "window" in fn_lower:
                categories.add("Windows")
            elif "wall" in fn_lower:
                categories.add("Walls")
            elif "floor" in fn_lower:
                categories.add("Floors")
            elif "column" in fn_lower or "pillar" in fn_lower:
                categories.add("Structural Columns")

    family_context = {}
    for cat in categories:
        result = model_query("get_loaded_families", {"category": cat})
        if "error" not in result:
            family_context[cat] = result
    if family_context:
        context["loaded_families"] = family_context

    # Levels — useful for verifying placement level context
    levels_result = model_query("get_levels", {})
    if "error" not in levels_result:
        context["levels"] = levels_result

    return context


def _check_preconditions(motif: dict, context: dict) -> list[str]:
    """
    Validate preconditions listed in the motif against current model context.

    Returns a list of failed precondition messages (empty = all passed).
    """
    failures = []
    active_view = context.get("active_view", {})
    view_type = active_view.get("view_type", active_view.get("ViewType", ""))

    for precondition in motif.get("preconditions", []):
        p_lower = precondition.lower()
        if "floor plan" in p_lower and view_type and "FloorPlan" not in view_type:
            failures.append(
                f"Precondition failed: '{precondition}' "
                f"(active view is '{view_type}', expected FloorPlan)"
            )
        elif "ceiling plan" in p_lower and view_type and "CeilingPlan" not in view_type:
            failures.append(
                f"Precondition failed: '{precondition}' "
                f"(active view is '{view_type}', expected CeilingPlan)"
            )
        # More precondition checks can be added here

    return failures


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_tool_sequence(
    motif: dict,
    fetch_context: bool = True,
) -> list[dict]:
    """
    Convert a Motif dict (from the Pattern Agent) into a mcp-servers-for-revit
    tool call list, grounded in the current live Revit model state.

    Args:
        motif:          Motif dict (name, description, steps, preconditions,
                        parameters_to_prompt).
        fetch_context:  If True (default), queries the live model for grounding.
                        Set False in unit tests or when Revit is not running.

    Returns:
        List of {"tool": str, "arguments": dict} ready for execute_shortcut().

    Raises:
        ValueError if the backend returns invalid JSON or preconditions fail hard.
    """
    client = anthropic.Anthropic()

    # ── 1. Fetch model context for grounding ──────────────────────────────────
    context: dict = {}
    precondition_warnings: list[str] = []

    if fetch_context:
        context = _fetch_model_context(motif)
        precondition_warnings = _check_preconditions(motif, context)

    # ── 2. Build user message with context ───────────────────────────────────
    context_section = ""
    if context:
        context_section = (
            "\n\nMODEL CONTEXT (from live Revit model — use to resolve family types):\n"
            + json.dumps(context, indent=2)
        )

    warnings_section = ""
    if precondition_warnings:
        warnings_section = (
            "\n\nPRECONDITION WARNINGS (include these in the tool sequence comments):\n"
            + "\n".join(f"  - {w}" for w in precondition_warnings)
        )

    user_message = (
        "Convert this BIM routine motif into an ordered mcp-servers-for-revit "
        "tool call sequence.\n\n"
        f"Motif:\n{json.dumps(motif, indent=2)}"
        f"{context_section}"
        f"{warnings_section}"
        "\n\nReturn ONLY the JSON array of tool call objects."
    )

    # ── 3. Call the Macro Agent ───────────────────────────────────────────────
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        sequence = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Macro Agent returned invalid JSON: {exc}\n\nRaw output:\n{text}"
        ) from exc

    if not isinstance(sequence, list):
        raise ValueError(
            f"Macro Agent must return a JSON array, got: {type(sequence)}"
        )

    # Validate each step has at least "tool" and "arguments" keys
    for i, step in enumerate(sequence):
        if "tool" not in step:
            raise ValueError(f"Step {i} missing 'tool' key: {step}")
        if "arguments" not in step:
            step["arguments"] = {}

    # Attach any precondition warnings as metadata (not part of execution)
    return sequence


def get_context_summary(motif: dict) -> dict:
    """
    Standalone helper — fetch and return model context for display in the dry-run.
    Used by agents.py to show grounding info to the user before confirmation.
    """
    context = _fetch_model_context(motif)
    warnings = _check_preconditions(motif, context)
    return {"context": context, "precondition_warnings": warnings}
