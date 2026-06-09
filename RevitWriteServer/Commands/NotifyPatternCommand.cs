using System.Text.Json.Nodes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using RevitWriteServer.Chat;

namespace RevitWriteServer.Commands;

/// <summary>
/// TCP command: notify_pattern
///
/// Called by RevitLogger/PatternBridge when a repeated routine is detected.
/// Opens the browser-based BIM Assistant chat at http://localhost:5000.
///
/// Flow:
///   1. PatternBridge (RevitLogger) sends TCP JSON-RPC → notify_pattern
///   2. This command POSTs the pattern to the Python chatbot server
///   3. If the server isn't running, starts chatbot/chat_server.py first
///   4. Opens the browser at http://localhost:5000
///   5. User chats with Claude, clicks Execute
///   6. Browser → POST /api/execute → revit_bridge.execute_shortcut()
///          → TCP JSON-RPC to this same port 8080
///          → PlaceElementCommand / SetParameterCommand / TagElementCommand
///          → each runs inside its own Transaction via CommandBase ExternalEvent
///
/// Required params: label (string), count (int), motif (object), tool_sequence (array)
/// </summary>
public class NotifyPatternCommand : CommandBase<string>
{
    public override string CommandName => "notify_pattern";

    private string _label        = "";
    private int    _count;
    private string _payloadJson  = "{}";   // pre-serialised in PrepareParameters (UI thread safe)

    public NotifyPatternCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters)
    {
        if (parameters is null)
            throw new ArgumentException("Parameters required for notify_pattern");

        _label = parameters["label"]?.GetValue<string>() ?? "Detected Routine";
        _count = parameters["count"]?.GetValue<int>()    ?? 0;

        // Serialise to string now, before any async hand-off
        var payload = new JsonObject
        {
            ["label"]         = _label,
            ["count"]         = _count,
            ["motif"]         = parameters["motif"]?.DeepClone(),
            ["tool_sequence"] = parameters["tool_sequence"]?.DeepClone(),
        };
        _payloadJson = payload.ToJsonString();
    }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        // Show and focus the dockable pane (must be on Revit API thread)
        try { WebChatPaneProvider.Pane?.Show(); } catch { /* pane might not be ready */ }

        // Start server + POST pattern + navigate WebView2 (background thread)
        var json = _payloadJson;
        _ = Task.Run(() => ChatbotLauncher.OpenAsync(json));
        _notifyResult = "ok";
    }

    protected override object GetResult() => new { status = _notifyResult ?? "ok" };
    private string? _notifyResult;
}
