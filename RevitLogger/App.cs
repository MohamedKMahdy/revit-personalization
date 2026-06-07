using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;
using System.Windows.Interop;

namespace RevitLogger;

/// <summary>
/// Revit IExternalApplication entry point.
///
/// Responsibilities:
///   • Subscribes to application-level DocumentChanged / lifecycle events.
///   • Manages one session (LogWriter + ActionCapture + RoutineDetector) per open project.
///   • Creates and owns the ShortcutRunner (IPC-based shortcut execution engine).
///   • Shows NotificationUI WPF toasts when RoutineDetector confirms a pattern.
///   • Launches the Python orchestrator when the user clicks "Learn as Shortcut".
///
/// Architecture compliance:
///   • RoutineDetector — real-time in-add-in pattern detection (thesis §4.1 ✓)
///   • ShortcutRunner  — file IPC execution channel (replaces read-only Autodesk MCP ✓)
///   • NotificationUI  — proactive WPF toast (thesis §4.1 notification UI ✓)
/// </summary>
[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public class App : IExternalApplication
{
    // docKey → per-document session triple
    private readonly Dictionary<string, (ActionCapture Capture, LogWriter Writer, RoutineDetector Detector)>
        _sessions = new();

    private ShortcutRunner? _shortcutRunner;

    // Root directory of the Python project — used to launch the orchestrator.
    // Matches the path used by deploy.ps1 and the VS Code workspace.
    private static readonly string ProjectDir = System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        "revit-personalization");

    // ── IExternalApplication ──────────────────────────────────────────────────

    public Result OnStartup(UIControlledApplication application)
    {
        DiagLog("=== RevitLogger OnStartup ===");

        // Initialise the shortcut runner (creates ExternalEvent + FileSystemWatcher).
        // Must be done here, inside a valid Revit API context, for ExternalEvent.Create().
        _shortcutRunner = new ShortcutRunner();
        _shortcutRunner.Initialize();

        var ctrl = application.ControlledApplication;
        ctrl.DocumentChanged  += OnDocumentChanged;
        ctrl.DocumentOpened   += OnDocumentOpened;
        ctrl.DocumentCreated  += OnDocumentCreated;
        ctrl.DocumentClosing  += OnDocumentClosing;

        DiagLog("OnStartup: event subscriptions registered");
        return Result.Succeeded;
    }

    public Result OnShutdown(UIControlledApplication application)
    {
        DiagLog("OnShutdown called");

        var ctrl = application.ControlledApplication;
        ctrl.DocumentChanged  -= OnDocumentChanged;
        ctrl.DocumentOpened   -= OnDocumentOpened;
        ctrl.DocumentCreated  -= OnDocumentCreated;
        ctrl.DocumentClosing  -= OnDocumentClosing;

        foreach (var (_, session) in _sessions)
        {
            session.Detector.RoutineDetected -= OnRoutineDetected;
            session.Capture.Dispose();
            session.Writer.Flush();
        }
        _sessions.Clear();

        _shortcutRunner?.Dispose();
        return Result.Succeeded;
    }

    // ── DocumentChanged ───────────────────────────────────────────────────────

    private void OnDocumentChanged(object? sender, DocumentChangedEventArgs e)
    {
        try
        {
            var doc = e.GetDocument();
            if (doc is null || doc.IsFamilyDocument) return;

            var key   = DocKey(doc);
            var found = _sessions.TryGetValue(key, out var session);

            var addedCount    = e.GetAddedElementIds()?.Count   ?? 0;
            var modifiedCount = e.GetModifiedElementIds()?.Count ?? 0;
            DiagLog($"OnDocumentChanged: key={key} found={found} added={addedCount} modified={modifiedCount}");

            if (found)
                session.Capture.ProcessEvent(e);
            else
                DiagLog($"  >> No session for key={key}. Known: [{string.Join(", ", _sessions.Keys)}]");
        }
        catch (Exception ex)
        {
            DiagLog($"OnDocumentChanged EXCEPTION: {ex.Message}");
        }
    }

    // ── Document lifecycle ────────────────────────────────────────────────────

    private void OnDocumentOpened(object? sender, DocumentOpenedEventArgs e)
    {
        DiagLog($"OnDocumentOpened: title={e.Document?.Title} path={e.Document?.PathName}");
        StartSession(e.Document, VersionName(sender));
    }

    private void OnDocumentCreated(object? sender, DocumentCreatedEventArgs e)
    {
        DiagLog($"OnDocumentCreated: title={e.Document?.Title}");
        StartSession(e.Document, VersionName(sender));
    }

    private void OnDocumentClosing(object? sender, DocumentClosingEventArgs e)
    {
        DiagLog($"OnDocumentClosing: title={e.Document?.Title}");
        EndSession(e.Document);
    }

    // ── Session management ────────────────────────────────────────────────────

    private void StartSession(Document? doc, string revitVersion)
    {
        if (doc is null || doc.IsFamilyDocument) return;

        var key = DocKey(doc);
        DiagLog($"StartSession: key={key} title={doc.Title}");

        if (_sessions.ContainsKey(key))
        {
            DiagLog($"  >> Session already exists for key={key}, skipping");
            return;
        }

        try
        {
            var sessionId = $"sess_{DateTime.UtcNow:yyyyMMddHHmmss}";
            var info      = SessionInfo.Create(doc, sessionId, revitVersion);
            var writer    = new LogWriter(sessionId, info.DocumentHash);
            writer.WriteSessionStart(info);

            var detector = new RoutineDetector();
            detector.RoutineDetected += OnRoutineDetected;

            var capture = new ActionCapture(writer, detector);
            _sessions[key] = (capture, writer, detector);

            DiagLog($"  >> Session started: id={sessionId} hash={info.DocumentHash}");
        }
        catch (Exception ex)
        {
            DiagLog($"StartSession EXCEPTION: {ex}");
        }
    }

    private void EndSession(Document? doc)
    {
        if (doc is null) return;
        var key = DocKey(doc);
        DiagLog($"EndSession: key={key}");

        if (!_sessions.TryGetValue(key, out var session)) return;

        session.Detector.RoutineDetected -= OnRoutineDetected;
        session.Capture.Dispose();
        session.Writer.Flush();
        _sessions.Remove(key);
    }

    // ── Routine detection → notification ─────────────────────────────────────

    /// <summary>
    /// Called on the Revit UI thread when RoutineDetector confirms a repeated pattern.
    /// Shows a WPF toast. DocumentChanged fires on the UI thread, so this is safe.
    /// </summary>
    private void OnRoutineDetected(object? sender, RoutineDetectedEventArgs args)
    {
        DiagLog($"OnRoutineDetected: id={args.Id} count={args.Count} label={args.Label}");
        try
        {
            var title = $"Routine Detected ({args.Count}× repeated)";
            var ui    = new NotificationUI(args.Id, title, args.Label, LaunchOrchestrator);

            // Set Revit as owner so the toast floats above Revit but below other apps
            var helper = new WindowInteropHelper(ui);
            helper.Owner = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle;

            ui.Show();
        }
        catch (Exception ex)
        {
            DiagLog($"OnRoutineDetected: NotificationUI error: {ex.Message}");
        }
    }

    /// <summary>
    /// Launches the Python orchestrator in a visible console window.
    /// The user can watch the agent pipeline produce a shortcut.
    /// </summary>
    private static void LaunchOrchestrator(string routineId)
    {
        try
        {
            DiagLog($"LaunchOrchestrator: routine_id={routineId}");
            var startInfo = new System.Diagnostics.ProcessStartInfo
            {
                FileName         = "cmd.exe",
                // /k keeps the window open after the script finishes so the user can read output
                Arguments        = $"/k python orchestrator/agents.py --routine-id {routineId} --k 5",
                WorkingDirectory = ProjectDir,
                UseShellExecute  = true,   // opens a new console window
            };
            System.Diagnostics.Process.Start(startInfo);
        }
        catch (Exception ex)
        {
            DiagLog($"LaunchOrchestrator error: {ex.Message}");
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    /// <summary>
    /// Stable document key — full lowercased path for saved docs, title for unsaved.
    /// Using the path string (not a hash) makes key comparison fully deterministic.
    /// </summary>
    private static string DocKey(Document doc)
    {
        if (!string.IsNullOrEmpty(doc.PathName))
            return doc.PathName.ToLowerInvariant();
        return "unsaved::" + doc.Title.ToLowerInvariant();
    }

    private static string VersionName(object? sender)
        => sender is Autodesk.Revit.ApplicationServices.Application a
            ? a.VersionName : "";

    // ── Diagnostics ───────────────────────────────────────────────────────────

    /// <summary>
    /// Appends a timestamped line to _diag.txt.  Thread-safe.  Never throws.
    /// </summary>
    internal static void DiagLog(string message)
    {
        try
        {
            var dir = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RevitPersonalization", "logs");
            System.IO.Directory.CreateDirectory(dir);
            var path = System.IO.Path.Combine(dir, "_diag.txt");
            lock (typeof(App))
                System.IO.File.AppendAllText(path, $"[{DateTime.UtcNow:HH:mm:ss.fff}] {message}\n");
        }
        catch { /* never let diagnostics crash the add-in */ }
    }
}
