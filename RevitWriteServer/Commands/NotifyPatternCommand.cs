using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
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

    private const string ChatbotUrl = "http://localhost:5000";

    // Shared HttpClient — one per process lifetime is the .NET recommendation
    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(4) };

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
        // Fire-and-forget: network I/O must not block the Revit API thread.
        // Execution (PlaceElement / SetParameter / TagElement) happens via
        // separate TCP commands dispatched by the Python chatbot server —
        // they each go through CommandBase's own ExternalEvent.
        var json  = _payloadJson;   // capture for closure
        _ = Task.Run(() => OpenBrowserAsync(json));

        _notifyResult = "ok";
    }

    private static async Task OpenBrowserAsync(string payloadJson)
    {
        var content = new StringContent(payloadJson, Encoding.UTF8, "application/json");

        // ── 1. Try to reach the already-running chatbot server ────────────────
        bool serverUp = false;
        try
        {
            using var resp = await _http.PostAsync($"{ChatbotUrl}/api/pattern", content);
            serverUp = resp.IsSuccessStatusCode;
        }
        catch { /* connection refused — server not running */ }

        // ── 2. Start the server if needed ─────────────────────────────────────
        if (!serverUp)
        {
            StartChatbotServer();

            // Poll up to 8 s for the server to come up
            content = new StringContent(payloadJson, Encoding.UTF8, "application/json");
            for (int i = 0; i < 16 && !serverUp; i++)
            {
                await Task.Delay(500);
                try
                {
                    content = new StringContent(payloadJson, Encoding.UTF8, "application/json");
                    using var resp = await _http.PostAsync($"{ChatbotUrl}/api/pattern", content);
                    serverUp = resp.IsSuccessStatusCode;
                }
                catch { }
            }
        }

        // ── 3. Open the browser (open even if server is slow — user can refresh) ──
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName        = ChatbotUrl,
                UseShellExecute = true,   // lets Windows pick the default browser
            });
        }
        catch { /* best-effort */ }
    }

    /// <summary>
    /// Launches chatbot/chat_server.py as a background process.
    /// Looks for the project root via the REVIT_PROJECT_DIR env var (from .env),
    /// then falls back to ~/revit-personalization.
    /// </summary>
    private static void StartChatbotServer()
    {
        var projectDir = DotEnvReader.GetApiKey("REVIT_PROJECT_DIR");

        if (string.IsNullOrWhiteSpace(projectDir))
        {
            var candidate = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "revit-personalization");
            if (Directory.Exists(candidate))
                projectDir = candidate;
        }

        if (string.IsNullOrWhiteSpace(projectDir))
            return;

        var scriptPath = Path.Combine(projectDir, "chatbot", "chat_server.py");
        if (!File.Exists(scriptPath))
            return;

        foreach (var pyExe in new[] { "python", "py", "python3" })
        {
            try
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName               = pyExe,
                    Arguments              = $"\"{scriptPath}\" --no-browser",
                    WorkingDirectory       = projectDir,
                    UseShellExecute        = false,
                    CreateNoWindow         = true,
                    RedirectStandardOutput = false,
                    RedirectStandardError  = false,
                });
                return;   // started successfully
            }
            catch { }
        }
    }

    protected override object GetResult() => new { status = _notifyResult ?? "ok" };
    private string? _notifyResult;
}
