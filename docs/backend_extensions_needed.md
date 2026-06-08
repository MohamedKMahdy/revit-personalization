# Backend Extensions Needed — mcp-servers-for-revit

Per thesis methodology §4: "If a needed operation is missing, add it as a
backend tool extension rather than working around it."

## Current Status (v1.0.0, commit 1a52de9)

| Required Operation | Backend Tool | Status |
|---|---|---|
| Place element | `create_point_based_element` | ✅ Available |
| Read family types | `get_available_family_types` | ✅ Available |
| Read active view | `get_current_view_info` | ✅ Available |
| Read selection | `get_selected_elements` | ✅ Available |
| Set parameter | `modify_element` (stub) | ❌ Empty stub in v1.0.0 |
| Tag individual element | None | ❌ Only tag_all_walls/rooms |
| Execute arbitrary C# | `send_code_to_revit` | ✅ Available (used as interim) |

## Interim Solution

`set_element_parameter` and `create_element_tag` are both implemented in
`revit_bridge.py` using `send_code_to_revit` with inline C# code. This is
validated by the `send_code_to_revit` tool having transaction mode support
(added in commit from March 2026).

## Planned Proper Extensions

### Extension 1: `set_element_parameter`

Add to the mcp-servers-for-revit fork as a new tool:

**TypeScript side** (`mcp-server/src/tools/set_element_parameter.ts`):
```typescript
server.tool("set_element_parameter",
  "Set a parameter value on a Revit element by name", {
  data: z.object({
    elementId: z.number().describe("Revit ElementId of the target element"),
    parameterName: z.string().describe("Parameter name (LookupParameter key)"),
    value: z.union([z.string(), z.number()]).describe("Value to set"),
  })
}, async (args) => { /* → sendCommand("set_element_parameter", args) */ });
```

**C# plugin side** (new `SetElementParameterCommand.cs`):
```csharp
public object Execute(Document doc, JObject args) {
    var elem = doc.GetElement(new ElementId((long)args["elementId"]));
    var param = elem.LookupParameter((string)args["parameterName"]);
    using var tx = new Transaction(doc, "Set Parameter");
    tx.Start();
    // Handle String / Double / Integer storage types
    tx.Commit();
    return new { status = "OK", elementId, parameterName, value };
}
```

### Extension 2: `create_element_tag`

Add as a new tool targeting a specific element (not all walls/rooms):

**TypeScript side** (`mcp-server/src/tools/create_element_tag.ts`):
```typescript
server.tool("create_element_tag",
  "Place an annotation tag on a specific Revit element", {
  data: z.object({
    elementId: z.number().describe("ElementId of the element to tag"),
    tagFamilyName: z.string().describe("Tag family name (partial match OK)"),
    tagOrientation: z.enum(["Horizontal", "Vertical"]).default("Horizontal"),
  })
}, async (args) => { /* → sendCommand("create_element_tag", args) */ });
```

### Extension 3: Revit 2027 support

The plugin currently targets .NET 8 for Revit 2025-2026.
The C# logging add-in (RevitLogger) targets Revit 2027 / .NET 10.

To add Revit 2027 support to the write plugin:
1. Fork the repo
2. Add a `revit-plugin-2027/` folder targeting .NET 10 and Revit 2027 API
3. The C# plugin code itself needs no changes — just a new build target

## Installation Steps (Current v1.0.0 for Revit 2026)

1. Download `revit-plugin-2026.zip` from:
   https://github.com/mcp-servers-for-revit/mcp-servers-for-revit/releases/tag/v1.0.0

2. Extract to:
   `%AppData%\Autodesk\Revit\Addins\2026\`

3. Open Revit 2026.

4. Validate connection:
   ```powershell
   $env:PYTHONPATH = '.'
   python -c "from mcp_server.revit_bridge import say_hello; print(say_hello())"
   ```
   Expected: dialog appears in Revit, returns `{"status": "Hello from Revit!"}`

5. Test placement:
   ```powershell
   python -c "
   from mcp_server.revit_bridge import model_query
   print(model_query('get_available_family_types', {'categoryList': ['OST_Doors'], 'limit': 5}))
   "
   ```
