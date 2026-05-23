"""
Macro / Command Agent — converts a Motif into a Revit MCP tool call sequence.

Model:  claude-sonnet-4-6  (fast, strong at structured config generation)
Output: list of {"tool": str, "arguments": dict} — the exact payload that
        revit_bridge.execute_mcp_tool_sequence() sends to the Revit Public MCP Server.

Runtime placeholder convention (resolved at execution time by the C# WPF shortcut UI):
  {{location}}          — user-clicked XYZ point
  {{last_element_id}}   — element_id of the most recently placed element
  {{ParamName}}         — user-supplied value for a variable parameter
"""
from __future__ import annotations

import json
import os
import anthropic

MODEL = os.environ.get("MACRO_AGENT_MODEL", "claude-sonnet-4-6")

# ── Revit Public MCP Server tool schema shown to the agent ────────────────────
REVIT_MCP_TOOLS = """
REVIT PUBLIC MCP SERVER — AVAILABLE TOOLS
==========================================

place_element(family_type: str, location: object) -> dict
  Places a family instance in the active view.
  family_type: full Revit family + type name, e.g. "Door-Passage-Single-Full_Lite:36\\" x 84\\""
  location: use the placeholder "{{location}}" (resolved to user click at runtime)

set_parameter(element_id: int | "{{last_element_id}}", parameter_name: str, value: any) -> dict
  Sets a parameter value on an element.
  element_id: use "{{last_element_id}}" to refer to the most recently placed element.
  value: use the literal value for constants; use "{{ParamName}}" for variable params.

create_annotation_tag(element_id: int | "{{last_element_id}}", tag_family: str) -> dict
  Places an annotation tag on an element.
  element_id: use "{{last_element_id}}" for the most recently tagged element.
  tag_family: Revit tag family name, e.g. "Door Tag".

PLACEHOLDER SUMMARY
-------------------
{{location}}          resolved to the user's clicked point when shortcut runs
{{last_element_id}}   resolved to the element_id of the last placed/modified element
{{SomeParamName}}     resolved to user input at runtime (for variable parameters)
"""

SYSTEM_PROMPT = f"""\
You are a Revit automation engineer. Given a generalised BIM routine motif, you \
produce a JSON array of MCP tool calls that will execute the routine against the \
Revit Public MCP Server.

{REVIT_MCP_TOOLS}

OUTPUT FORMAT
Return ONLY a JSON array — no markdown, no explanation:

[
  {{"tool": "<tool_name>", "arguments": {{<key>: <value>, ...}}}},
  ...
]

MAPPING RULES
1. Place step    → place_element; location = "{{{{location}}}}"
2. SetParam step → set_parameter; element_id = "{{{{last_element_id}}}}";
                   if param_value_type is "constant": value = the literal param_value
                   if param_value_type is "variable":  value = "{{{{<param_name>}}}}"
3. Tag step      → create_annotation_tag; element_id = "{{{{last_element_id}}}}";
                   tag_family = the tag_family_name from the motif step
4. Preserve step order exactly as given in the motif.
5. Do not invent steps not present in the motif.
"""


def generate_tool_sequence(motif: dict) -> list[dict]:
    """
    Convert a Motif dict (from the Pattern Agent) into a Revit MCP tool call list.

    Args:
        motif: Motif dict with keys: name, description, steps, preconditions,
               parameters_to_prompt.

    Returns:
        List of {"tool": str, "arguments": dict} ready for execute_mcp_tool_sequence().
    """
    client = anthropic.Anthropic()

    user_message = (
        "Convert this BIM routine motif into an ordered MCP tool call sequence.\n\n"
        f"Motif:\n{json.dumps(motif, indent=2)}\n\n"
        "Return ONLY the JSON array of tool call objects."
    )

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
        raise ValueError(f"Macro Agent must return a JSON array, got: {type(sequence)}")

    # Validate each step has at least "tool" and "arguments" keys
    for i, step in enumerate(sequence):
        if "tool" not in step:
            raise ValueError(f"Step {i} missing 'tool' key: {step}")
        if "arguments" not in step:
            step["arguments"] = {}

    return sequence
