using System.Reflection;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.UI;

namespace BIMAssistant;

/// <summary>
/// Standalone "BIM Assistant" Revit add-in — a pure chat-UI host.
///
/// It registers a dockable pane containing an embedded WebView2 pointed at the local
/// chatbot server (http://127.0.0.1:5000) plus a ribbon button to open it. It does
/// NOT log, observe, or write the model. Every Revit action the assistant performs
/// flows through mcp-servers-for-revit (the chatbot's /api/execute → revit_bridge →
/// the plugin on localhost:8080), so this add-in's only job is to render the chat.
///
/// Logging lives in the separate generalBIMlog add-in; execution in the
/// mcp-servers-for-revit plugin. This is the third, independent piece.
/// </summary>
[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public class App : IExternalApplication
{
    // The single dockable-pane content instance, created at startup and reused.
    internal static AssistantPane? AssistantPaneInstance { get; private set; }

    public Result OnStartup(UIControlledApplication application)
    {
        DiagLog("=== BIM Assistant OnStartup ===");
        CreateRibbonPanel(application);
        RegisterAssistantPane(application);
        return Result.Succeeded;
    }

    public Result OnShutdown(UIControlledApplication application)
    {
        try
        {
            AssistantPaneInstance?.DisposeWebView();
            ChatServer.Stop();   // kill the chatbot server this add-in launched
        }
        catch (Exception ex) { DiagLog($"OnShutdown error: {ex.Message}"); }
        return Result.Succeeded;
    }

    // ── Ribbon ────────────────────────────────────────────────────────────────

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
                "BIMAssistant.OpenAssistantCommand")
            {
                ToolTip = "Open the embedded BIM Personalization chat assistant in a dockable pane.",
                AvailabilityClassName = "BIMAssistant.AssistantAvailability",
            };
            panel.AddItem(btn);
            DiagLog("Ribbon panel created");
        }
        catch (Exception ex) { DiagLog($"Ribbon setup failed (non-fatal): {ex.Message}"); }
    }

    /// <summary>
    /// Registers the "BIM Assistant" dockable pane. Must run from OnStartup — Revit
    /// only accepts pane registration at application startup.
    /// </summary>
    private static void RegisterAssistantPane(UIControlledApplication application)
    {
        try
        {
            AssistantPaneInstance = new AssistantPane();
            application.RegisterDockablePane(AssistantPane.PaneId, "BIM Assistant", AssistantPaneInstance);
            DiagLog("Dockable pane registered");
        }
        catch (Exception ex)
        {
            AssistantPaneInstance = null;   // lets the command distinguish "reg failed" from "server down"
            DiagLog($"Dockable pane registration FAILED: {ex}");
        }
    }

    // ── Diagnostics (never throws) ───────────────────────────────────────────

    internal static void DiagLog(string message)
    {
        try
        {
            var dir = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RevitPersonalization", "logs");
            System.IO.Directory.CreateDirectory(dir);
            var path = System.IO.Path.Combine(dir, "_assistant.txt");
            lock (typeof(App))
                System.IO.File.AppendAllText(path, $"[{DateTime.UtcNow:HH:mm:ss.fff}] {message}\n");
        }
        catch { /* diagnostics must never crash the add-in */ }
    }
}
