"""
Pattern Agent — extracts a generalized routine motif from k example episodes.

Model:  claude-opus-4-8  (extended thinking for deep parameter analysis)
Output: Motif JSON matching shared.schemas.Motif / MotifStep field names.

Jang & Lee (2023) framing: the motif captures the invariant action sequence and
distinguishes constant parameter values from variable ones, exactly as required
for reproducibility analysis (§4.2 "parameter rule extraction").
"""
from __future__ import annotations

import json
import os
import anthropic

MODEL = os.environ.get("PATTERN_AGENT_MODEL", "claude-opus-4-8")

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are a BIM workflow analyst specialising in Autodesk Revit authoring behaviour.
You receive k recorded examples of the same user routine — each is an ordered list
of BIM authoring actions (Place, SetParam, Tag) — and you extract a generalised
"motif": a reusable template that captures the invariant pattern.

OUTPUT FORMAT
Return ONLY a single valid JSON object, no markdown fences, no explanation:

{
  "name": "<short human-readable name, e.g. 'Place Door + Mark + Tag'>",
  "description": "<one sentence describing what this routine achieves>",
  "steps": [
    {
      "action_type": "Place",
      "family_name": "<Revit family name, e.g. 'Door-Passage-Single-Full_Lite'>",
      "param_name": "",
      "param_value": null,
      "param_value_type": "",
      "tag_family_name": ""
    },
    {
      "action_type": "SetParam",
      "family_name": "",
      "param_name": "<parameter name, e.g. 'Mark'>",
      "param_value": "<literal value if constant, null if it varies>",
      "param_value_type": "constant | variable",
      "tag_family_name": ""
    },
    {
      "action_type": "Tag",
      "family_name": "",
      "param_name": "",
      "param_value": null,
      "param_value_type": "",
      "tag_family_name": "<tag family name, e.g. 'Door Tag'>"
    }
  ],
  "preconditions": [
    "<e.g. 'Active view must be a floor plan'>",
    "<e.g. 'A host wall must exist at the placement point'>"
  ],
  "parameters_to_prompt": [
    "<param_name of every SetParam step whose param_value_type is variable>"
  ]
}

ANALYSIS RULES
1. Include a step for every action type that appears in ALL examples.
2. If an action appears in most but not all examples, include it and note it in
   the description as "usually performed".
3. For SetParam steps:
   - If param_value_after is IDENTICAL across all examples → param_value_type = "constant",
     param_value = that literal value.
   - If param_value_after DIFFERS across examples → param_value_type = "variable",
     param_value = null, and add param_name to parameters_to_prompt.
4. Use the view_type field to infer preconditions (FloorPlan → floor plan required).
5. Use element_category and family_name from the Place step for the step's family_name.
6. Keep names concise and human-readable.
"""


def extract_motif(examples: list[dict], routine_label: str = "") -> dict:
    """
    Run the Pattern Agent over k example RoutineExample dicts.

    Args:
        examples: List of RoutineExample.model_dump() dicts, each with an
                  "actions" key containing a list of ActionRecord dicts.
        routine_label: CandidateRoutine.label for additional context.

    Returns:
        Motif dict with keys: name, description, steps, preconditions,
        parameters_to_prompt — ready to pass to Motif(**result).
    """
    client = anthropic.Anthropic()

    # Slim down the action dicts — only send fields the agent needs
    KEEP_FIELDS = {
        "action_type", "element_category", "family_name", "type_name",
        "param_name", "param_value_before", "param_value_after",
        "tag_family_name", "tagged_element_id",
        "level_name", "view_type", "transaction_name",
    }

    slim_examples = []
    for i, ex in enumerate(examples):
        actions = ex.get("actions", [])
        slim_actions = [{k: v for k, v in a.items() if k in KEEP_FIELDS and v not in (None, "", 0)}
                        for a in actions]
        slim_examples.append({"example": i + 1, "actions": slim_actions})

    user_message = (
        f"Here are {len(examples)} recorded examples of the Revit routine "
        f'"{routine_label}". '
        "Extract a generalised motif that captures the invariant pattern.\n\n"
        f"Examples:\n{json.dumps(slim_examples, indent=2)}\n\n"
        "Return ONLY the JSON motif object."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract the text block (skip thinking blocks)
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text.strip()
            break

    if not text:
        raise ValueError("Pattern Agent returned no text content")

    # Strip markdown code fences if present
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        motif = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Pattern Agent returned invalid JSON: {exc}\n\nRaw output:\n{text}") from exc

    # Validate required keys are present
    required = {"name", "description", "steps", "preconditions", "parameters_to_prompt"}
    missing = required - motif.keys()
    if missing:
        raise ValueError(f"Pattern Agent motif missing required keys: {missing}\n\nGot: {motif}")

    return motif
