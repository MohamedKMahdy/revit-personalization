using Autodesk.Revit.UI;
using RevitWriteServer.Commands;

namespace RevitWriteServer;

/// <summary>
/// IExternalApplication entry point.
///
/// On startup:
///  1. Defers TCP server start to ApplicationInitialized (when UIApplication is available).
///
/// The TCP server accepts JSON-RPC 2.0 commands from the Python orchestrator.
/// When a pattern is detected, NotifyPatternCommand starts the Python chatbot
/// server (chatbot/chat_server.py) and opens the browser at http://localhost:5000.
/// </summary>
public class App : IExternalApplication
{
    private TcpCommandServer? _server;

    public Result OnStartup(UIControlledApplication application)
    {
        try
        {
            // ── Ribbon tab: "BIM Assistant" ───────────────────────────────────
            // Adds an "Open BIM Assistant" button so the user can open the
            // browser chat at any time, not just when a pattern fires.
            try { application.CreateRibbonTab("BIM Assistant"); } catch { /* already exists */ }

            var panel   = application.CreateRibbonPanel("BIM Assistant", "Pattern Shortcuts");
            var btnData = new PushButtonData(
                "OpenBIMAssistant",
                "Open BIM\nAssistant",
                typeof(App).Assembly.Location,
                typeof(OpenBIMAssistantCommand).FullName!);
            btnData.ToolTip = "Open the BIM Assistant chat in your browser (localhost:5000).\n" +
                              "The Python chatbot server starts automatically if not running.";
            btnData.LongDescription =
                "Detected a repeated modeling routine? Click here to review it " +
                "and optionally execute it as a one-click shortcut.";
            panel.AddItem(btnData);

            // ── Defer TCP server to ApplicationInitialized ────────────────────
            application.ControlledApplication.ApplicationInitialized += OnApplicationInitialized;
            return Result.Succeeded;
        }
        catch (Exception ex)
        {
            ShowError("RevitWriteServer startup failed", ex);
            return Result.Failed;
        }
    }

    private void OnApplicationInitialized(
        object? sender,
        Autodesk.Revit.DB.Events.ApplicationInitializedEventArgs _)
    {
        try
        {
            var app = new UIApplication(sender as Autodesk.Revit.ApplicationServices.Application);
            StartServer(app);
        }
        catch (Exception ex)
        {
            ShowError("RevitWriteServer failed to start TCP server", ex);
        }
    }

    private void StartServer(UIApplication uiApp)
    {
        // Instantiate all commands (each creates its own ExternalEvent)
        var sayHello        = new SayHelloCommand(uiApp);
        var getFamilyTypes  = new GetFamilyTypesCommand(uiApp);
        var getViewInfo     = new GetViewInfoCommand(uiApp);
        var getSelected     = new GetSelectedElementsCommand(uiApp);
        var placeElement    = new PlaceElementCommand(uiApp);
        var setParameter    = new SetParameterCommand(uiApp);
        var tagElement      = new TagElementCommand(uiApp);
        var notifyPattern   = new NotifyPatternCommand(uiApp);

        // No static refs needed — execution goes Python → TCP → CommandBase path

        // Register all commands with the TCP server
        _server = new TcpCommandServer();
        _server.RegisterCommand(sayHello);
        _server.RegisterCommand(getFamilyTypes);
        _server.RegisterCommand(getViewInfo);
        _server.RegisterCommand(getSelected);
        _server.RegisterCommand(placeElement);
        _server.RegisterCommand(setParameter);
        _server.RegisterCommand(tagElement);
        _server.RegisterCommand(notifyPattern);   // ← opens the chat panel

        _server.Start();

        uiApp.Application.WriteJournalComment(
            "RevitWriteServer: TCP server started on localhost:8080 | BIM Assistant browser UI on localhost:5000",
            true);
    }

    public Result OnShutdown(UIControlledApplication application)
    {
        try
        {
            _server?.Stop();
            _server?.Dispose();
        }
        catch { /* best-effort */ }
        return Result.Succeeded;
    }

    private static void ShowError(string title, Exception ex) =>
        TaskDialog.Show(title, $"{ex.GetType().Name}: {ex.Message}");
}
