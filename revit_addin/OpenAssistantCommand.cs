using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Diagnostics;
using System.IO;
using System.Net.Http;

namespace RevitLogger;

/// <summary>
/// Ribbon button command — starts the Python chatbot server if not already running,
/// then opens http://localhost:5000 in the default browser.
/// </summary>
[Transaction(TransactionMode.ReadOnly)]
public class OpenAssistantCommand : IExternalCommand
{
    private const string ChatbotUrl = "http://localhost:5000";

    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        try
        {
            EnsureServerRunning();
            Process.Start(new ProcessStartInfo(ChatbotUrl) { UseShellExecute = true });
            return Result.Succeeded;
        }
        catch (Exception ex)
        {
            message = $"Failed to open BIM Assistant: {ex.Message}";
            App.DiagLog($"OpenAssistantCommand EXCEPTION: {ex}");
            return Result.Failed;
        }
    }

    private static void EnsureServerRunning()
    {
        // Quick connectivity check — if it answers, nothing to do.
        if (IsServerUp()) return;

        // Read PYTHON_EXE and REPO_ROOT from the local .env
        var envPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "RevitPersonalization", ".env");

        if (!File.Exists(envPath))
        {
            App.DiagLog("EnsureServerRunning: .env not found, skipping server start");
            return;
        }

        string? pythonExe = null, repoRoot = null;
        foreach (var line in File.ReadAllLines(envPath))
        {
            var parts = line.Split('=', 2);
            if (parts.Length != 2) continue;
            var key = parts[0].Trim();
            var val = parts[1].Trim();
            if (key == "PYTHON_EXE") pythonExe = val;
            if (key == "REPO_ROOT")  repoRoot  = val;
        }

        if (pythonExe == null || repoRoot == null)
        {
            App.DiagLog("EnsureServerRunning: PYTHON_EXE or REPO_ROOT missing from .env");
            return;
        }

        var serverScript = Path.Combine(repoRoot, "chatbot", "chat_server.py");
        if (!File.Exists(serverScript))
        {
            App.DiagLog($"EnsureServerRunning: server script not found at {serverScript}");
            return;
        }

        App.DiagLog($"EnsureServerRunning: starting server via {pythonExe}");
        Process.Start(new ProcessStartInfo
        {
            FileName         = pythonExe,
            Arguments        = $"\"{serverScript}\" --no-browser",
            UseShellExecute  = false,
            CreateNoWindow   = true,
            WorkingDirectory = repoRoot,
        });

        // Wait up to 6 s for the server to become ready.
        for (int i = 0; i < 12; i++)
        {
            System.Threading.Thread.Sleep(500);
            if (IsServerUp()) return;
        }
        App.DiagLog("EnsureServerRunning: server did not respond within 6 s");
    }

    private static bool IsServerUp()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            var response = client.GetAsync(ChatbotUrl).GetAwaiter().GetResult();
            return response.IsSuccessStatusCode;
        }
        catch { return false; }
    }
}
