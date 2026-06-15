// ============================================================================
// ShortcutRunner.cs — RETIRED (thesis §4.1 architecture update)
//
// This class previously implemented an IPC-based shortcut execution engine:
//   • Watched ipc/pending_execution.json via FileSystemWatcher
//   • Read ShortcutConfig, ran Place / SetParam / Tag in a Revit transaction
//   • Wrote back execution_result_{id}.json for the Python server to read
//
// Under the updated methodology the C# add-in is OBSERVER ONLY.
// All model writes are now delegated to mcp-servers-for-revit:
//   https://github.com/simonmoreau/mcp-servers-for-revit
//
// Execution flow (new):
//   Python MCP server (execute_revit_command)
//     -> revit_bridge.execute_shortcut()
//       -> mcp-servers-for-revit (place_element / set_parameter / create_annotation_tag)
//         -> Revit API inside the TypeScript plugin's external event handler
//
// This file is retained as a stub so the project compiles without changes to
// the .csproj.  It contains no functional code.
// ============================================================================

namespace RevitLogger
{
    // Intentionally empty — execution is now handled by mcp-servers-for-revit.
    // See revit_bridge.py and mcp_server/server.py (execute_revit_command tool).
}
