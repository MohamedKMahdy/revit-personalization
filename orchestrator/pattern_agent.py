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

from shared import llm  # noqa: E402

MODEL = llm.pick("PATTERN_AGENT_MODEL", "claude-opus-4-8")

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

RICHER WORKFLOWS (OPTIONAL — only when the EXAMPLES clearly show it; never invent structure)
Most routines are a flat single-element Place→SetParam→Tag and need NONE of the fields below.
Add them ONLY when the evidence in the examples demands it (a downstream validator will strip any
claim the examples do not support, so unsupported structure is wasted):
- MULTI-ELEMENT COMPOUND: if an example places TWO OR MORE distinct elements (e.g. a wall then a
  door on it), set "workflow_type":"compound", list the elements in top-level "elements":
  [{"role":"wall","family":"Basic Wall"},{"role":"door","family":"M_Door...","host":"wall"}], and on
  each step add "element_role" (which element it acts on) and, for a hosted Place, "host_role".
- LOOP: if an example places the SAME family multiple times in a row (e.g. doors spaced along a
  wall, marks incrementing D-01, D-02, ...), set "workflow_type":"loop" and on the repeated Place step
  add "repeat": {"over":"<what is iterated, e.g. selected wall>","spacing_mm":<n or omit>,
  "index_param":"Mark","mark_expr":"D-{i:02}"}.
- CONDITIONAL: if a SetParam value depends on a property (e.g. frame='Wide' only when width>1500),
  add "condition":"<guard>" to that step.
- COMPUTED VALUE: if a SetParam value is derived rather than literal/sequence (e.g. width=2*height,
  or the host room's number), set "value_expr":"<expression>" instead of a literal param_value.
All of these are OPTIONAL keys on the existing step/motif shape; omit them for ordinary flat routines.
"""


def _validate_and_downgrade(motif: dict, examples: list[dict]) -> dict:
    """Deterministic safety net against over-generalization: strip any richer-workflow claim the
    EXAMPLES do not support, falling back to a flat motif. The LLM may propose loops/compounds/
    conditions; this keeps only the ones the recorded evidence actually backs (a guarded failure is a
    publishable boundary finding, not a silently-wrong automation). Records what it stripped in
    `_downgrade_notes` for transparency."""
    place_fams: list[list[str]] = []      # families placed, per example
    param_values: dict[str, set] = {}     # param_name -> distinct values seen across examples
    for ex in examples:
        fams: list[str] = []
        for a in ex.get("actions", []):
            at = a.get("action_type")
            if at == "Place":
                f = a.get("family_name") or a.get("element_category") or ""
                if f:
                    fams.append(f)
            elif at == "SetParam" and a.get("param_name"):
                v = a.get("param_value_after", a.get("param_value_before"))
                param_values.setdefault(a["param_name"], set()).add("" if v is None else str(v))
        place_fams.append(fams)

    motif.setdefault("workflow_type", "linear")   # normalize so downstream always sees the field
    supports_compound = any(len(set(f)) >= 2 for f in place_fams)               # >=2 distinct elements
    supports_loop = any((max((f.count(x) for x in set(f)), default=0) >= 2)     # same family placed >=2x
                        for f in place_fams)
    notes: list[str] = []

    if motif.get("workflow_type") == "compound" and not supports_compound:
        motif["workflow_type"] = "linear"
        notes.append("workflow_type compound->linear (examples show a single element)")
    if motif.get("elements") and not supports_compound:
        motif["elements"] = []
        notes.append("dropped 'elements' (no multi-element evidence)")
    if motif.get("workflow_type") == "loop" and not supports_loop:
        motif["workflow_type"] = "linear"
        notes.append("workflow_type loop->linear (no repeated placement in examples)")

    for s in motif.get("steps", []):
        if s.get("repeat") and not supports_loop:
            s.pop("repeat", None)
            notes.append("stripped a step 'repeat' (no repeated placement in examples)")
        if not supports_compound:
            s.pop("element_role", None)
            s.pop("host_role", None)
        pn = s.get("param_name")
        if (s.get("condition") or s.get("value_expr")) and pn and len(param_values.get(pn, set())) <= 1:
            s.pop("condition", None)
            s.pop("value_expr", None)
            notes.append(f"stripped condition/value_expr on constant param '{pn}'")

    if notes:
        motif["_downgrade_notes"] = notes
    return motif


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
    client = llm.client(MODEL)

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

    create_kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    if llm.supports_thinking(MODEL):            # `thinking` is Claude-only — omit it for Gemini
        create_kwargs["thinking"] = {"type": "adaptive"}
    response = client.messages.create(**create_kwargs)

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

    # Deterministic guard: keep only the richer-workflow structure the examples actually support.
    return _validate_and_downgrade(motif, examples)
