using System.Reflection;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;

namespace RevitLogger;

/// <summary>
/// Revit IExternalApplication entry point.
///
/// Responsibilities (thesis §4.1 — Observer-Only Add-in):
///   • Subscribes to application-level DocumentChanged / lifecycle events.
///   • Manages one session (LogWriter + ActionCapture + RoutineDetector) per open project.
///   • When RoutineDetector fires, hands the detected pattern to the Python BIM Assistant
///     chatbot via PatternBridge (no user click needed).
///
/// Detection → assistant flow (fully automatic):
///   1. User models in Revit (place element, set params, tag).
///   2. DocumentChanged fires → ActionCapture.ProcessEvent → RoutineDetector.Feed.
///   3. After ≥ 2 identical episodes: RoutineDetected event fires on the UI thread.
///   4. OnRoutineDetected starts Task.Run → PatternBridge.NotifyAsync (background).
///   5. PatternBridge writes pending_pattern.json and launches chatbot/notify_from_file.py.
///   6. That starts the FastAPI chat server (if needed) and POSTs the pattern to :5000.
///   7. The BIM Assistant opens in the browser and Claude greets the user.
///   No toast, no button clicks.
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
        ctrl.DocumentSaving   += OnDocumentSaving;
        ctrl.DocumentSavedAs  += OnDocumentSavedAs;

        application.ViewActivated += OnViewActivated;

        CreateRibbonPanel(application);

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
        ctrl.DocumentSaving   -= OnDocumentSaving;
        ctrl.DocumentSavedAs  -= OnDocumentSavedAs;

        application.ViewActivated -= OnViewActivated;

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
            var deletedCount  = e.GetDeletedElementIds()?.Count  ?? 0;
            DiagLog($"OnDocumentChanged: key={key} found={found} added={addedCount} modified={modifiedCount} deleted={deletedCount}");

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

    private void OnDocumentSaving(object? sender, DocumentSavingEventArgs e)
    {
        // The LogWriter flushes to disk after every record, so no explicit flush is needed.
        // Log a checkpoint marker for session timeline analysis.
        DiagLog($"OnDocumentSaving: title={e.Document?.Title}");
    }

    private void OnDocumentSavedAs(object? sender, DocumentSavedAsEventArgs e)
    {
        // The session key is doc.CreationGUID which does NOT change on SaveAs,
        // so no re-keying is needed. Just log it for diagnostics.
        DiagLog($"OnDocumentSavedAs: title={e.Document?.Title} newPath={e.Document?.PathName}");
    }

    private void OnViewActivated(object? sender, ViewActivatedEventArgs e)
    {
        // Track view switches for multi-project diagnostics.
        // The DocKey (CreationGUID) is stable even when the user switches views,
        // so we don't need to update session mappings here.
        DiagLog($"OnViewActivated: view='{e.CurrentActiveView?.Name}' doc='{e.CurrentActiveView?.Document?.Title}'");
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

    // ── Routine detection → BIM Assistant chatbot ─────────────────────────────

    /// <summary>
    /// Called on the Revit UI thread when RoutineDetector confirms a repeated pattern.
    ///
    /// Fires PatternBridge on a background thread — NOT inline here.
    /// PatternBridge does file I/O and launches a Python process, which must not run
    /// on the Revit UI thread.
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
    /// Stable document key using doc.CreationGUID — a Guid assigned once at project
    /// creation that survives SaveAs, path moves, and round-trips through Revit.
    /// Falls back to the path or title for edge cases where the API is unavailable.
    ///
    /// Matches the supervisor's generalBIMlog ProjectFileManager keying strategy.
    /// </summary>
    private static string DocKey(Document doc)
    {
        try
        {
            var guid = doc.CreationGUID;
            if (guid != Guid.Empty) return guid.ToString();
        }
        catch { /* fallback for unusual document states */ }

        if (!string.IsNullOrEmpty(doc.PathName))
            return doc.PathName.ToLowerInvariant();
        return "unsaved::" + doc.Title.ToLowerInvariant();
    }

    private static string VersionName(object? sender)
        => sender is Autodesk.Revit.ApplicationServices.Application a
            ? a.VersionName : "";

    // ── Ribbon panel ─────────────────────────────────────────────────────────

    private static void CreateRibbonPanel(UIControlledApplication application)
    {
        try
        {
            const string TabName = "BIM Personalization";
            try { application.CreateRibbonTab(TabName); } catch { /* already exists */ }

            var panel = application.CreateRibbonPanel(TabName, "BIM Assistant");

            var btn = new PushButtonData(
                "OpenAssistant",
                "Open\nAssistant",
                Assembly.GetExecutingAssembly().Location,
                "RevitLogger.OpenAssistantCommand")
            {
                ToolTip = "Start the BIM Personalization chat assistant and open it in the browser.",
            };

            panel.AddItem(btn);
            DiagLog("OnStartup: BIM Personalization ribbon panel created");
        }
        catch (Exception ex)
        {
            DiagLog($"OnStartup: Ribbon setup failed (non-fatal): {ex.Message}");
        }
    }

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
