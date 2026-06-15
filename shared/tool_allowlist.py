"""
Execution-safety allowlist — the SINGLE source of truth for which tools the
personalization pipeline (Pattern → Macro agent → bridge) may emit and dispatch.

This is an ENFORCED, code-level constraint, not a prompt instruction. It is
checked at three chokepoints:

  1. orchestrator/macro_agent.generate_tool_sequence — validates the LLM's output
  2. mcp_server/revit_bridge.execute_shortcut        — validates before any step runs
  3. mcp_server/revit_bridge._dispatch_tool          — per-step backstop right
                                                       before the TCP call to Revit

The pipeline speaks a deliberately small, bounded vocabulary. Arbitrary code
execution (send_code_to_revit / Roslyn) is NEVER reachable through this path —
deny-by-default: anything not explicitly permitted is rejected.

To add a capability you must edit BOTH this allowlist and the dispatch mapping in
revit_bridge._dispatch_tool — there is no implicit passthrough.
"""
from __future__ import annotations

# Abstract pipeline tool names the Macro Agent is permitted to emit. Each maps to
# a bounded, named Tier 1–3 backend operation in revit_bridge._dispatch_tool:
#   place_element         -> create_point_based_element        (Tier 1)
#   set_parameter         -> set_element_parameter             (Tier 1)
#   create_annotation_tag -> tag_element                       (Tier 1)
PERMITTED_PIPELINE_TOOLS: frozenset[str] = frozenset({
    "place_element",
    "set_parameter",
    "create_annotation_tag",
})

# Explicitly named denies — deny-by-default already blocks these, but listing the
# arbitrary-code tool makes violations self-documenting in logs and tests.
EXPLICITLY_FORBIDDEN_TOOLS: frozenset[str] = frozenset({
    "send_code_to_revit",
})


class DisallowedToolError(ValueError):
    """Raised when the pipeline attempts a tool outside the allowlist."""


def is_permitted(tool: str) -> bool:
    """True iff `tool` is in the permitted pipeline vocabulary."""
    return tool in PERMITTED_PIPELINE_TOOLS


def assert_tool_allowed(tool: str) -> None:
    """Raise DisallowedToolError unless `tool` is permitted (deny-by-default)."""
    if tool not in PERMITTED_PIPELINE_TOOLS:
        reason = (
            "explicitly forbidden (arbitrary code execution)"
            if tool in EXPLICITLY_FORBIDDEN_TOOLS
            else "not in the permitted tool set"
        )
        raise DisallowedToolError(
            f"Tool '{tool}' is {reason}. "
            f"Permitted tools: {sorted(PERMITTED_PIPELINE_TOOLS)}."
        )


def validate_tool_sequence(sequence: list[dict]) -> None:
    """
    Validate a whole tool-call sequence. Raise DisallowedToolError listing every
    offending step if any names a non-permitted tool (e.g. send_code_to_revit).
    """
    offending: list[tuple[int, str]] = []
    for i, step in enumerate(sequence):
        tool = (step or {}).get("tool", "")
        if tool not in PERMITTED_PIPELINE_TOOLS:
            offending.append((i, tool))
    if offending:
        details = ", ".join(f"step {i}: '{t or '<missing>'}'" for i, t in offending)
        raise DisallowedToolError(
            f"Rejected tool sequence — {len(offending)} disallowed call(s): {details}. "
            f"Permitted tools: {sorted(PERMITTED_PIPELINE_TOOLS)}. "
            f"Arbitrary code execution (e.g. send_code_to_revit) is never permitted."
        )
