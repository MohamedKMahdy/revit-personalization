using System.IO;
using System.Net.Http;

namespace BIMAssistant;

/// <summary>
/// Locates and launches the Python chatbot server (chatbot/chat_server.py) and probes
/// whether it is serving. Shared by OpenAssistantCommand and AssistantPane.
///
/// Config is read from %LOCALAPPDATA%\RevitPersonalization\.env (written by
/// setup_revit_env.py):  PYTHON_EXE + REPO_ROOT.
///
/// The chatbot itself executes Revit actions through mcp-servers-for-revit
/// (revit_bridge → localhost:8080); this class only starts/serves the chat UI.
/// </summary>
internal static class ChatServer
{
    // Bind/probe on 127.0.0.1 explicitly. The Python server binds 127.0.0.1 (IPv4);
    // probing "localhost" can resolve to ::1 (IPv6) first and fail on some machines.
    public const string Url = "http://127.0.0.1:5000";

    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(2) };

    // Serializes launches so a concurrent command+pane first-open can't spawn two servers.
    private static readonly object _launchGate = new();
    // The server process this add-in launched (null if a server was already running).
    private static System.Diagnostics.Process? _serverProc;

    /// <summary>True if the server answers a GET / with a success status.</summary>
    public static bool IsUp()
    {
        try
        {
            var resp = _http.GetAsync(Url).GetAwaiter().GetResult();
            return resp.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    /// <summary>
    /// Ensures the server is running. Returns true if it is up (already or after start).
    /// Safe to call repeatedly and from multiple threads — launches are serialized and
    /// re-checked under a lock. Never call on the Revit UI thread: it can block up to
    /// <paramref name="waitSeconds"/>.
    /// </summary>
    public static bool EnsureRunning(int waitSeconds = 12)
    {
        if (IsUp()) return true;

        lock (_launchGate)
        {
            if (IsUp()) return true;   // another thread may have started it while we waited

            if (!TryReadEnv(out var pythonExe, out var repoRoot))
            {
                App.DiagLog("ChatServer: .env missing PYTHON_EXE/REPO_ROOT — run setup_revit_env.py");
                return false;
            }

            var serverScript = Path.Combine(repoRoot, "chatbot", "chat_server.py");
            if (!File.Exists(serverScript))
            {
                App.DiagLog($"ChatServer: server script not found at {serverScript}");
                return false;
            }

            var launcher = PreferPythonw(pythonExe);   // pythonw.exe → no console flash
            App.DiagLog($"ChatServer: starting via {launcher}");
            try
            {
                _serverProc = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                {
                    FileName         = launcher,
                    Arguments        = $"\"{serverScript}\" --no-browser",
                    UseShellExecute  = false,
                    CreateNoWindow   = true,
                    WorkingDirectory = repoRoot,
                });
                App.DiagLog($"ChatServer: launched PID {_serverProc?.Id}");
            }
            catch (Exception ex)
            {
                App.DiagLog($"ChatServer: launch failed: {ex.Message}");
                return false;
            }

            for (int i = 0; i < waitSeconds * 2; i++)
            {
                System.Threading.Thread.Sleep(500);
                if (IsUp()) return true;
                if (_serverProc is { HasExited: true })
                {
                    App.DiagLog($"ChatServer: process exited early (code {_serverProc.ExitCode}) before binding");
                    return false;
                }
            }
            App.DiagLog($"ChatServer: did not respond within {waitSeconds}s");
            return false;
        }
    }

    /// <summary>Kills the server this add-in launched (if any). Called from App.OnShutdown.</summary>
    public static void Stop()
    {
        try
        {
            if (_serverProc is { HasExited: false })
            {
                _serverProc.Kill(entireProcessTree: true);
                App.DiagLog($"ChatServer: stopped PID {_serverProc.Id}");
            }
        }
        catch (Exception ex)
        {
            App.DiagLog($"ChatServer: Stop failed: {ex.Message}");
        }
        finally { _serverProc = null; }
    }

    private static string PreferPythonw(string pythonExe)
    {
        try
        {
            if (pythonExe.EndsWith("python.exe", StringComparison.OrdinalIgnoreCase))
            {
                var w = pythonExe.Substring(0, pythonExe.Length - "python.exe".Length) + "pythonw.exe";
                if (File.Exists(w)) return w;
            }
        }
        catch { /* fall through to the configured interpreter */ }
        return pythonExe;
    }

    private static bool TryReadEnv(out string pythonExe, out string repoRoot)
    {
        pythonExe = ""; repoRoot = "";
        var envPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "RevitPersonalization", ".env");
        if (!File.Exists(envPath)) return false;

        foreach (var line in File.ReadAllLines(envPath))
        {
            var parts = line.Split('=', 2);
            if (parts.Length != 2) continue;
            var key = parts[0].Trim();
            var val = parts[1].Trim();
            if (key == "PYTHON_EXE") pythonExe = val;
            else if (key == "REPO_ROOT") repoRoot = val;
        }
        return pythonExe.Length > 0 && repoRoot.Length > 0;
    }
}
