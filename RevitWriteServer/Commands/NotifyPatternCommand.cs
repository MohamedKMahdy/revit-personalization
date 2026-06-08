using System.Text.Json;
using System.Text.Json.Nodes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using RevitWriteServer.Chat;

namespace RevitWriteServer.Commands;

/// <summary>
/// TCP command: notify_pattern
///
/// Called by the Python orchestrator when a repeated pattern is detected.
/// Shows the dockable chat panel and starts the Claude conversation.
/// The ExecuteCallback inside PatternData runs the shortcut directly via
/// RunOnUIThread — no TCP round-trip needed since we are already in-process.
///
/// Required params:
///   label         (string) — human-readable routine name
///   count         (int)    — how many times detected
///   motif         (object) — structured motif JSON
///   tool_sequence (array)  — list of MCP tool-call steps
/// </summary>
public class NotifyPatternCommand : CommandBase<string>
{
    public override string CommandName => "notify_pattern";

    // Set by App.cs after all commands are instantiated
    public static ChatPaneProvider? PaneProvider { get; set; }
    public static PlaceElementCommand? PlaceCmd { get; set; }
    public static SetParameterCommand? SetParamCmd { get; set; }
    public static TagElementCommand? TagCmd { get; set; }
    public static GetFamilyTypesCommand? FamilyTypesCmd { get; set; }

    private string _label = "";
    private int _count;
    private JsonNode? _motif;
    private JsonArray? _toolSequence;

    public NotifyPatternCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters)
    {
        if (parameters is null)
            throw new ArgumentException("Parameters required for notify_pattern");

        _label        = parameters["label"]?.GetValue<string>() ?? "Detected Routine";
        _count        = parameters["count"]?.GetValue<int>()    ?? 0;
        _motif        = parameters["motif"];
        _toolSequence = parameters["tool_sequence"]?.AsArray();
    }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        if (PaneProvider?.Panel is null)
        {
            _notifyResult = "error: panel not available";
            return;
        }

        // Capture for closure — doc must not escape the UI thread
        var capturedDoc = doc;
        var capturedSeq = _toolSequence;

        var data = new PatternData
        {
            Label         = _label,
            Count         = _count,
            Motif         = _motif,
            ToolSequence  = capturedSeq,
            ExecuteCallback = () => ExecuteToolSequence(capturedDoc, capturedSeq),
        };

        // Show the dockable panel and load the pattern
        UiApp.GetDockablePane(ChatPaneProvider.PanelId).Show();
        PaneProvider.Panel.LoadPattern(data);

        _notifyResult = "ok";
    }

    protected override object GetResult() => new { status = _notifyResult ?? "ok" };
    private string? _notifyResult;

    // ── Direct shortcut execution (called from the WPF UI thread) ────────────

    private static void ExecuteToolSequence(Document doc, JsonArray? toolSequence)
    {
        if (toolSequence is null || toolSequence.Count == 0)
            throw new InvalidOperationException("Empty tool sequence");

        long lastElementId = -1;

        using var tx = new Transaction(doc, "BIM Assistant: Execute Shortcut");
        tx.Start();

        foreach (var stepNode in toolSequence)
        {
            if (stepNode is null) continue;

            var tool = stepNode["tool"]?.GetValue<string>() ?? "";
            var args = stepNode["arguments"]?.DeepClone() as JsonObject
                       ?? new JsonObject();

            // Resolve {{last_element_id}} placeholder
            if (lastElementId > 0)
            {
                foreach (var key in args.Select(p => p.Key).ToList())
                {
                    if (args[key]?.GetValue<string>() == "{{last_element_id}}")
                        args[key] = lastElementId;
                }
            }

            var result = DispatchStep(tool, args, doc, lastElementId);

            // Track placed element ID for chaining
            if (result is PlaceElementResult per)
                lastElementId = per.ElementId;
            else if (result is System.Text.Json.JsonElement je &&
                     je.TryGetProperty("elementId", out var eid))
                lastElementId = eid.GetInt64();
        }

        tx.Commit();
    }

    private static object? DispatchStep(string tool, JsonObject args, Document doc, long lastId)
    {
        switch (tool)
        {
            case "place_element":
            {
                // Resolve family_type name → typeId if not already provided
                if (!args.ContainsKey("typeId") && args["family_type"]?.GetValue<string>() is string fname)
                {
                    var resolvedId = ResolveFamilyTypeId(doc, fname);
                    if (resolvedId.HasValue) args["typeId"] = resolvedId.Value;
                }
                // Map location object {x,y,z} to flat params
                if (args["location"] is JsonObject loc)
                {
                    args["x"] = loc["x"]?.GetValue<double>() ?? 0;
                    args["y"] = loc["y"]?.GetValue<double>() ?? 0;
                    args["z"] = loc["z"]?.GetValue<double>() ?? 0;
                    args.Remove("location");
                }
                return PlaceCmd?.RunOnUIThread(args, doc);
            }

            case "set_parameter":
            {
                // Normalise snake_case → camelCase
                Rename(args, "element_id",    "elementId");
                Rename(args, "parameter_name","parameterName");
                // If element_id was a placeholder that resolved to -1, skip
                if (args["elementId"]?.GetValue<long?>() is -1 or null && lastId > 0)
                    args["elementId"] = lastId;
                return SetParamCmd?.RunOnUIThread(args, doc);
            }

            case "create_annotation_tag":
            {
                Rename(args, "element_id", "elementId");
                if (args["elementId"]?.GetValue<long?>() is -1 or null && lastId > 0)
                    args["elementId"] = lastId;
                return TagCmd?.RunOnUIThread(args, doc);
            }

            default:
                return null;
        }
    }

    private static void Rename(JsonObject obj, string from, string to)
    {
        if (!obj.ContainsKey(from) || obj.ContainsKey(to)) return;
        var val = obj[from]?.DeepClone();
        obj.Remove(from);
        if (val is not null) obj[to] = val;
    }

    private static long? ResolveFamilyTypeId(Document doc, string familyName)
    {
        var lower = familyName.ToLowerInvariant();
        return new FilteredElementCollector(doc)
            .OfClass(typeof(FamilySymbol))
            .Cast<FamilySymbol>()
            .FirstOrDefault(sym =>
                $"{sym.FamilyName} {sym.Name}".ToLowerInvariant().Contains(lower))
            ?.Id.Value;
    }
}
