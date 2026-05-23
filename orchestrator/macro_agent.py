"""Macro/Command Agent — converts a motif into an MCP tool call sequence."""
from __future__ import annotations
import json
import anthropic

MODEL = "claude-sonnet-4-6"

REVIT_MCP_TOOL_SCHEMA = """
Available Revit Public MCP Server tools:
- place_element(family_type: str, location: object) → places a family instance
- set_parameter(element_id: int|"{{last_element_id}}", parameter_name: str, value: any)
- create_annotation_tag(element_id: int|"{{last_element_id}}", tag_family: str)
- delete_element(element_id: int)

Special runtime placeholders:
- "{{location}}" — resolved to user-clicked point at execution time
- "{{last_element_id}}" — resolved to the element_id of the most recently placed element
- "{{ParameterName}}" — resolved to user input at execution time (for variable params)
"""

SYSTEM_PROMPT = f"""\
You are a Revit automation engineer. Given a motif (a generalized BIM routine), \
you produce a JSON MCP tool call sequence that executes the routine against the \
Revit Public MCP Server.

{REVIT_MCP_TOOL_SCHEMA}

Rules:
1. For Place steps: use place_element with "{{{{location}}}}" as the location placeholder
2. For SetParam steps: use set_parameter; reference the just-placed element with \
"{{{{last_element_id}}}}"; if paramValueType is "constant" use the literal value; \
if "variable" use "{{{{{paramName}}}}}" as the value placeholder
3. For Tag steps: use create_annotation_tag with "{{{{last_element_id}}}}"
4. Output ONLY a JSON array of tool call objects: \
[{{"tool": "...", "arguments": {{...}}}}]
5. No markdown, no explanation, just the JSON array.
"""


def generate_tool_sequence(motif: dict) -> list[dict]:
    """
    Convert a motif into a list of Revit MCP tool calls.

    Args:
        motif: Motif dict from the Pattern Agent.

    Returns:
        List of tool call dicts ready for execute_revit_command.
    """
    client = anthropic.Anthropic()

    user_message = f"""\
Convert this BIM routine motif into an MCP tool call sequence.

Motif:
{json.dumps(motif, indent=2)}

Return ONLY the JSON array of tool calls.
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Macro Agent returned invalid JSON: {e}\n\nRaw: {text}")
