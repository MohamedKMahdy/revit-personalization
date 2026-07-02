"""
Shared data models used by the MCP server, orchestrator, and log reader.

Field names use snake_case throughout — matching the JSON keys written by the
C# RevitLogger add-in (ActionRecord.cs).  The synthetic test files under
tests/synthetic_logs/ also use these names.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

# Place = model-element instantiation; Create = non-model creation (views, sheets, viewports,
# dimensions — discriminated by operation_class + element_category); Modify = geometry/relationship
# edits (move / join / align). Create + Modify widen the pipeline BEYOND element instantiation
# (Track D): documentation and modification routines become detectable and learnable, not just
# place-set-tag. Additive — existing logs, motifs, and tests are untouched.
ActionType = Literal["Place", "SetParam", "Tag", "Delete", "Create", "Modify"]
OperationClass = Literal["Model", "Parameter", "Annotation", "View"]


class ActionRecord(BaseModel):
    """
    One atomic BIM authoring event — the core log unit.

    Schema follows Jang & Lee (2023) arXiv:2305.18032 enhanced BIM logging:
      • transaction_id / transaction_name  — groups records within one Revit transaction
      • param_value_before / param_value_after — before/after diffs for reproducibility
      • level_name, phase_name, view_type   — rich spatial and temporal context

    Field names match the JSON keys produced by the C# RevitLogger add-in.
    """
    # ── Schema / session identity ─────────────────────────────────────────
    schema_version: str = "2.0"
    event_id:       str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id:     str = ""
    transaction_id: str = ""
    transaction_name: str = ""

    # ── Timing ───────────────────────────────────────────────────────────
    timestamp_utc:  str   = ""
    timestamp_unix: float = Field(default_factory=time.time)

    # ── Action taxonomy (Jang et al. 2023 AEI lexicon) ───────────────────
    action_type:     ActionType     = "Place"
    operation_class: OperationClass = "Model"

    # ── Element context ───────────────────────────────────────────────────
    element_id:       int = 0
    element_category: str = ""
    family_name:      str = ""
    type_name:        str = ""
    level_name:       str = ""
    phase_name:       str = ""
    host_category:    str = ""

    # ── View context ──────────────────────────────────────────────────────
    view_id:   int = 0
    view_name: str = ""
    view_type: str = ""

    # ── SetParam fields ───────────────────────────────────────────────────
    param_name:         str | None = None
    param_group:        str | None = None
    param_storage_type: str | None = None
    param_value_before: Any        = None
    param_value_after:  Any        = None

    # ── Tag / Annotation fields ───────────────────────────────────────────
    tag_family_name:  str | None = None
    tagged_element_id: int | None = None

    # ── Convenience aliases (used in log_reader.py) ───────────────────────
    @property
    def action(self) -> str:
        """Alias for action_type — used throughout log_reader for brevity."""
        return self.action_type

    @property
    def timestamp(self) -> float:
        """Alias for timestamp_unix."""
        return self.timestamp_unix

    class Config:
        # Allow extra fields from future schema versions without crashing
        extra = "ignore"


class RoutineExample(BaseModel):
    """One recorded repetition of a candidate routine."""
    example_id:  str
    session_id:  str
    recorded_at: float
    actions:     list[ActionRecord]


class CandidateRoutine(BaseModel):
    """
    A detected candidate routine with all its recorded examples.

    Two independent ranking axes are carried on every candidate:
      • support    — cluster size / how many example instances were grouped
                     (the FREQUENCY signal). `count` is a backward-compatible
                     alias holding the same value.
      • confidence — quality of the grouping. NOTE the meaning differs by detector:
                       v0.1 (SubstringDetector): frequency-based  min(1, count/5)
                       v0.2 (ClusterDetector):   mean pairwise intra-cluster
                                                 similarity (TIGHTNESS), 0–1.
                     Because these are different axes, do not compare a v0.1
                     confidence against a v0.2 confidence as if equivalent — the
                     v0.1-vs-v0.2 evaluation compares detection precision/recall
                     against the labeled session, not the confidence value.
    """
    id:               str
    label:            str            # e.g. "Place(M_Single-Flush) → SetParam×4 → Tag"
    action_signature: str            # compact e.g. "P,S,S,S,S,T"
    count:            int            # cluster size (frequency) — alias of `support`
    support:          int = 0        # cluster size (frequency signal); set == count
    confidence:       float = 0.0    # tightness (v0.2) or frequency (v0.1) — see class doc
    examples:         list[RoutineExample] = []


class MotifStep(BaseModel):
    """One step in a generalised routine motif.

    The base fields (action_type/family_name/param_name/...) describe a FLAT single-element step
    — the original, still-default shape. The OPTIONAL fields below let a step express the richer
    workflows people actually repeat; all default to empty so existing flat motifs are unchanged:
      • element_role : which element of a multi-element routine this step acts on (e.g. "door"),
                       so later steps can refer back to it instead of "the placed element".
      • host_role    : for a hosted Place, the role of the element to host on (e.g. door host="wall").
      • condition    : a guard, e.g. "width>1500" — the step only runs when it holds.
      • value_expr   : a SetParam computed value instead of a literal, e.g. "2*height" or
                       "room.number" (the agent evaluates it against the live model at runtime).
      • repeat       : a loop spec, e.g. {"over":"selected_walls","spacing_mm":2000,
                       "index_param":"Mark","mark_expr":"D-{i:02}"} or {"count":5} — run the step
                       once per item, advancing the index expression each time.
    """
    action_type:      ActionType = "Place"
    family_name:      str = ""   # Place only
    param_name:       str = ""   # SetParam only
    param_value:      Any = None # SetParam only; None = prompt user at runtime
    param_value_type: str = ""   # "constant" | "variable"
    tag_family_name:  str = ""   # Tag only
    # ── Richer-workflow extensions (all optional; empty = today's flat behaviour) ──
    element_role:     str = ""            # which element this step acts on (multi-element routines)
    host_role:        str = ""            # role of the host element for a hosted Place
    condition:        str = ""            # guard expression; step runs only if it holds
    value_expr:       str = ""            # SetParam computed value (vs a literal param_value)
    repeat:           dict | None = None  # loop spec (over / count / spacing_mm / index_param / mark_expr)


class Motif(BaseModel):
    """
    Generalised representation of a routine — output of the Pattern Agent.
    Contains the invariant step structure and which parameters vary per use.

    `workflow_type` + `elements` are optional richer-workflow metadata (default "linear" / empty,
    preserving the original flat single-element motif):
      • workflow_type : "linear" | "compound" | "loop" | "conditional" — how to read the steps.
      • elements      : the distinct elements of a compound routine and their host relationships,
                        e.g. [{"role":"wall","family":"Basic Wall"},
                              {"role":"door","family":"M_Door...","host":"wall"}].
    """
    name:                 str
    description:          str
    steps:                list[MotifStep]
    preconditions:        list[str] = []
    parameters_to_prompt: list[str] = []  # param_names that the user must supply
    workflow_type:        str = "linear"  # linear | compound | loop | conditional
    elements:             list[dict] = [] # roles + host relationships for multi-element routines
    # The routine's inferred INTENT — the latent the agent uses to understand (not just replay) the
    # routine: a HYPOTHESIS of WHY and WHEN, to be confirmed with the user (Stage 3), never auto-applied.
    #   {"goal": "a schedule-ready tagged door", "trigger": "a door placed with no Mark",
    #    "downstream": "the door schedule"}
    intent:               dict | None = None


class ShortcutConfig(BaseModel):
    """A named, saved shortcut ready for one-click execution."""
    shortcut_id:       str
    name:              str
    motif:             Motif
    mcp_tool_sequence: list[dict]
    created_at:        float = Field(default_factory=time.time)
