"""Pattern Agent — extracts a generalized motif from k example sequences."""
from __future__ import annotations
import json
import anthropic

MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """\
You are a BIM workflow analyst specializing in Autodesk Revit. You analyze sequences \
of user actions recorded in a Revit session and extract a generalized "motif" — a \
reusable template that captures the essential pattern across multiple examples.

A motif must include:
- Ordered steps (action type + any constant values)
- Which parameter values are constants (same in every example) vs. variables (differ per use)
- Preconditions (e.g., "active view must be a floor plan")
- A list of parameters the user should be prompted for at runtime

Output ONLY valid JSON matching this schema exactly:
{
  "name": "short human-readable name",
  "description": "one sentence",
  "steps": [
    {
      "action": "Place|SetParam|Tag|Delete",
      "familyType": "...",        // Place only; "" if N/A
      "paramName": "...",         // SetParam only; "" if N/A
      "paramValue": "...",        // SetParam only; null if variable
      "paramValueType": "constant|variable|pattern",  // SetParam only; "" if N/A
      "tagFamily": "..."          // Tag only; "" if N/A
    }
  ],
  "preconditions": ["..."],
  "parameters_to_prompt": ["param names that vary"]
}
"""


def extract_motif(examples: list[dict], routine_label: str = "") -> dict:
    """
    Run the Pattern Agent over k example sequences.

    Args:
        examples: List of example dicts, each with an "actions" key.
        routine_label: Optional label for context.

    Returns:
        Motif dict matching shared.schemas.Motif.
    """
    client = anthropic.Anthropic()

    examples_text = json.dumps(
        [{"example": i + 1, "actions": ex.get("actions", ex)} for i, ex in enumerate(examples)],
        indent=2,
    )

    user_message = f"""\
Here are {len(examples)} recorded examples of the same Revit routine \
{f'("{routine_label}") ' if routine_label else ''}by the same user.

Each example is a sequence of actions the user performed. Your task is to extract \
a generalized motif that captures the invariant pattern across all examples.

Examples:
{examples_text}

Analyze all examples carefully:
1. Identify which action types appear in every example in the same order
2. For SetParam steps: if paramValue is IDENTICAL across all examples → "constant"; \
if it differs → "variable" (and add paramName to parameters_to_prompt)
3. Identify any preconditions apparent from the viewId or category fields
4. Name the routine concisely

Return ONLY the JSON motif object, no other text.
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": 8000},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract JSON from the response (skip thinking blocks)
    for block in response.content:
        if block.type == "text":
            text = block.text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            try:
                return json.loads(text.strip())
            except json.JSONDecodeError as e:
                raise ValueError(f"Pattern Agent returned invalid JSON: {e}\n\nRaw: {text}")

    raise ValueError("Pattern Agent returned no text block")
