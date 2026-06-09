using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;

namespace RevitWriteServer.Chat;

/// <summary>
/// Shared helper that starts the Python chatbot server and opens the browser.
///
/// Used by:
///   - NotifyPatternCommand   — when a pattern is detected (POSTs pattern data first)
///   - OpenBIMAssistantCommand — when the user clicks the ribbon button (just opens browser)
/// </summary>
public static class ChatbotLauncher
{
    public const string ChatbotUrl = "http://localhost:5000";

    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(4) };

    // ── Public API ────────────────────────────────────────────────────────────

    /// <summary>
    /// Ensures the chatbot server is running, optionally POSTs a pattern payload,
    /// then opens the default browser at ChatbotUrl.
    /// </summary>
    public static async Task OpenAsync(string? patternJson = null)
    {
        bool serverUp = await IsServerUpAsync();

        if (!serverUp)
        {
            StartServer();
            // Poll up to 8 s
            for (int i = 0; i < 16 && !serverUp; i++)
            {
                await Task.Delay(500);
                serverUp = await IsServerUpAsync();
            }
        }

        // POST the pattern if provided (can retry now that server is up)
        if (patternJson is not null)
        {
            try
            {
                var content = new StringContent(patternJson, Encoding.UTF8, "application/json");
                await _http.PostAsync($"{ChatbotUrl}/api/pattern", content);
            }
            catch { /* best-effort */ }
        }

        // Open browser (even if server was slow — user can refresh)
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName        = ChatbotUrl,
                UseShellExecute = true,
            });
        }
        catch { }
    }

    // ── Internal helpers ──────────────────────────────────────────────────────

    private static async Task<bool> IsServerUpAsync()
    {
        try
        {
            using var resp = await _http.GetAsync($"{ChatbotUrl}/api/pattern");
            return resp.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    /// <summary>
    /// Finds and launches chatbot/chat_server.py as a background process.
    /// Looks for the project root via REVIT_PROJECT_DIR env var (from .env),
    /// then falls back to ~/revit-personalization.
    /// </summary>
    public static void StartServer()
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
                });
                return;
            }
            catch { }
        }
    }
}
