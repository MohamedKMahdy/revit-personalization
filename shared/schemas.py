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

ActionType = Literal["Place", "SetParam", "Tag", "Delete"]
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
    """A detected candidate routine with all its recorded examples."""
    id:               str
    label:            str            # e.g. "Place(M_Single-Flush) → SetParam×4 → Tag"
    action_signature: str            # compact e.g. "P,S,S,S,S,T"
    count:            int
    confidence:       float = 0.0   # 0–1: consistency across examples
    examples:         list[RoutineExample] = []


class MotifStep(BaseModel):
    """One step in a generalised routine motif."""
    action_type:      ActionType = "Place"
    family_name:      str = ""   # Place only
    param_name:       str = ""   # SetParam only
    param_value:      Any = None # SetParam only; None = prompt user at runtime
    param_value_type: str = ""   # "constant" | "variable"
    tag_family_name:  str = ""   # Tag only


class Motif(BaseModel):
    """
    Generalised representation of a routine — output of the Pattern Agent.
    Contains the invariant step structure and which parameters vary per use.
    """
    name:                 str
    description:          str
    steps:                list[MotifStep]
    preconditions:        list[str] = []
    parameters_to_prompt: list[str] = []  # param_names that the user must supply


class ShortcutConfig(BaseModel):
    """A named, saved shortcut ready for one-click execution."""
    shortcut_id:       str
    name:              str
    motif:             Motif
    mcp_tool_sequence: list[dict]
    created_at:        float = Field(default_factory=time.time)
