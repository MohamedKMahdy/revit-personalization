using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;

namespace RevitLogger;

/// <summary>
/// Revit IExternalApplication entry point.
///
/// Owns the application-level DocumentChanged subscription and dispatches each
/// event to the correct per-document <see cref="ActionCapture"/> session.
/// One session (LogWriter + ActionCapture) is created per opened project document
/// and torn down cleanly when the document closes or Revit shuts down.
/// </summary>
[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public class App : IExternalApplication
{
    // docKey → (capture session, log writer)
    private readonly Dictionary<string, (ActionCapture Capture, LogWriter Writer)> _sessions = new();

    public Result OnStartup(UIControlledApplication application)
    {
        DiagLog("=== RevitLogger OnStartup ===");

        var ctrl = application.ControlledApplication;

        // DocumentChanged is application-level; we dispatch to the right session.
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

        foreach (var (_, (capture, writer)) in _sessions)
        {
            capture.Dispose();
            writer.Flush();
        }
        _sessions.Clear();
        return Result.Succeeded;
    }

    // ── DocumentChanged ───────────────────────────────────────────────────

    private void OnDocumentChanged(object? sender, DocumentChangedEventArgs e)
    {
        try
        {
            var doc = e.GetDocument();
            if (doc is null || doc.IsFamilyDocument) return;

            var key = DocKey(doc);
            var found = _sessions.TryGetValue(key, out var session);

            var addedCount    = e.GetAddedElementIds()?.Count   ?? 0;
            var modifiedCount = e.GetModifiedElementIds()?.Count ?? 0;
            DiagLog($"OnDocumentChanged: key={key} found={found} added={addedCount} modified={modifiedCount} title={doc.Title}");

            if (found)
                session.Capture.ProcessEvent(e);
            else
                DiagLog($"  >> No session found for key={key}. Known keys: [{string.Join(", ", _sessions.Keys)}]");
        }
        catch (Exception ex)
        {
            DiagLog($"OnDocumentChanged EXCEPTION: {ex.Message}");
        }
    }

    // ── Document lifecycle ────────────────────────────────────────────────

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

    // ── Session management ────────────────────────────────────────────────

    private void StartSession(Document? doc, string revitVersion)
    {
        if (doc is null || doc.IsFamilyDocument) return;

        var key = DocKey(doc);
        DiagLog($"StartSession: key={key} title={doc.Title} path={doc.PathName}");

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

            var capture = new ActionCapture(writer);
            _sessions[key] = (capture, writer);

            DiagLog($"  >> Session started: sessionId={sessionId} docHash={info.DocumentHash}");
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

        session.Capture.Dispose();
        session.Writer.Flush();
        _sessions.Remove(key);
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    /// <summary>
    /// Stable document key. Saved docs use the lowercased path;
    /// unsaved docs use the title. Using the path string directly (not a hash)
    /// makes key comparison fully deterministic within the process.
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

    // ── Diagnostics ───────────────────────────────────────────────────────

    /// <summary>
    /// Writes a timestamped line to _diag.txt alongside the session logs.
    /// Safe to call from any thread; uses a lock for exclusive file access.
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
