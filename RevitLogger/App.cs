using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;

namespace RevitLogger;

/// <summary>
/// Revit IExternalApplication entry point.
///
/// Responsibilities (thesis §4.1 — Observer-Only Add-in):
///   • Subscribes to application-level DocumentChanged / lifecycle events.
///   • Manages one session (LogWriter + ActionCapture + RoutineDetector) per open project.
///   • When RoutineDetector fires, forwards the detected pattern to RevitWriteServer
///     via PatternBridge (direct TCP call — no Python, no user click needed).
///
/// Detection → panel flow (fully automatic):
///   1. User models in Revit (place element, set params, tag).
///   2. DocumentChanged fires → ActionCapture.ProcessEvent → RoutineDetector.Feed.
///   3. After ≥ 2 identical episodes: RoutineDetected event fires on the UI thread.
///   4. OnRoutineDetected starts Task.Run → PatternBridge.NotifyAsync (background).
///   5. PatternBridge sends notify_pattern to RevitWriteServer TCP on localhost:8080.
///   6. RevitWriteServer raises ExternalEvent → NotifyPatternCommand runs on UI thread.
///   7. BIM Assistant dockable panel activates and Claude greets the user.
///   No toast, no Python, no button clicks.
/// </summary>
[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public class App : IExternalApplication
{
    // docKey → per-document session triple
    private readonly Dictionary<string, (ActionCapture Capture, LogWriter Writer, RoutineDetector Detector)>
        _sessions = new();

    // ── IExternalApplication ──────────────────────────────────────────────────

    public Result OnStartup(UIControlledApplication application)
    {
        DiagLog("=== RevitLogger OnStartup (observer-only mode) ===");

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

    // ── Routine detection → BIM Assistant panel ───────────────────────────────

    /// <summary>
    /// Called on the Revit UI thread when RoutineDetector confirms a repeated pattern.
    ///
    /// Fires PatternBridge on a background thread — NOT inline here.
    /// PatternBridge makes a TCP call to RevitWriteServer which raises an ExternalEvent.
    /// ExternalEvents need the UI thread to dispatch, so if we blocked here we'd deadlock.
    /// </summary>
    private void OnRoutineDetected(object? sender, RoutineDetectedEventArgs args)
    {
        DiagLog($"OnRoutineDetected: id={args.Id} count={args.Count} label='{args.Label}'");

        // LatestEpisode is a fresh List<ActionRecord> copy (see RoutineDetector.FinalizeEpisode),
        // so it is safe to hand off to a background task without any locking.
        _ = Task.Run(() => PatternBridge.NotifyAsync(args));
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
