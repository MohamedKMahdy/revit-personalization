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

    // ── Dedicated ExternalEvent for shortcut execution ────────────────────────
    //
    // ExecuteCallback must NOT start a Transaction directly from the WPF handler:
    // being on the WPF UI thread is NOT the same as being in the Revit API context.
    // Transactions require an ExternalEvent or IExternalCommand frame.
    //
    // Flow:
    //   1. User clicks Execute (WPF thread)
    //   2. ExecuteCallback raises _execEvent (non-blocking) and returns a Task
    //   3. await in DoExecute() yields the message loop
    //   4. Revit dispatches _execEvent on the API thread → Transaction succeeds
    //   5. TaskCompletionSource signals → DoExecute continues → shows result
    private static ExecuteShortcutHandler? _execHandler;
    private static ExternalEvent?          _execEvent;

    public NotifyPatternCommand(UIApplication uiApp) : base(uiApp)
    {
        // Create the ExternalEvent once (constructor runs on the API thread)
        if (_execEvent is null)
        {
            _execHandler = new ExecuteShortcutHandler();
            _execEvent   = ExternalEvent.Create(_execHandler);
        }
    }

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
            Label        = _label,
            Count        = _count,
            Motif        = _motif,
            ToolSequence = capturedSeq,

            // Async callback: arms the ExternalEvent handler with the tool sequence,
            // raises the event (non-blocking), and returns a Task that completes when
            // the handler finishes.  DoExecute() awaits this, which yields the WPF
            // message loop so Revit can dispatch the event in the proper API context.
            ExecuteCallback = () =>
            {
                var tcs = new TaskCompletionSource<bool>();
                _execHandler!.Arm(capturedDoc, capturedSeq, tcs);
                var status = _execEvent!.Raise();
                if (status != ExternalEventRequest.Accepted)
                    return Task.FromException(
                        new InvalidOperationException($"ExternalEvent not accepted: {status}"));
                return tcs.Task;
            },
        };

        // Bring the panel to focus and load the pattern
        // (Show() is a no-op if already visible; it will un-minimise if collapsed)
        try { UiApp.GetDockablePane(ChatPaneProvider.PanelId).Show(); }
        catch { /* panel might already be visible */ }
        PaneProvider.Panel.LoadPattern(data);

        _notifyResult = "ok";
    }

    protected override object GetResult() => new { status = _notifyResult ?? "ok" };
    private string? _notifyResult;

    // ── ExternalEvent handler for shortcut execution ──────────────────────────

    /// <summary>
    /// Runs ExecuteToolSequence inside the Revit API context (ExternalEvent frame).
    /// Signals the TaskCompletionSource when done so the awaiting WPF handler can
    /// update the UI with success or failure.
    /// </summary>
    private sealed class ExecuteShortcutHandler : IExternalEventHandler
    {
        private Document? _doc;
        private JsonArray? _seq;
        private TaskCompletionSource<bool>? _tcs;

        public void Arm(Document doc, JsonArray? seq, TaskCompletionSource<bool> tcs)
        {
            _doc = doc;
            _seq = seq;
            _tcs = tcs;
        }

        void IExternalEventHandler.Execute(UIApplication app)
        {
            var doc = _doc;
            var seq = _seq;
            var tcs = _tcs;
            _doc = null;
            _seq = null;
            _tcs = null;

            if (doc is null || tcs is null) return;

            try
            {
                ExecuteToolSequence(doc, seq);
                tcs.SetResult(true);
            }
            catch (Exception ex)
            {
                tcs.SetException(ex);
            }
        }

        string IExternalEventHandler.GetName() => "BIM Assistant: Execute Shortcut";
    }

    // ── Tool sequence execution (always called inside ExternalEvent context) ──
    //
    // Each command (PlaceElementCommand, SetParameterCommand, TagElementCommand)
    // manages its own Transaction internally. Do NOT wrap this in an outer
    // Transaction — Revit forbids nested Transaction objects and will throw.

    private static void ExecuteToolSequence(Document doc, JsonArray? toolSequence)
    {
        if (toolSequence is null || toolSequence.Count == 0)
            throw new InvalidOperationException("Empty tool sequence");

        long lastElementId = -1;

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
