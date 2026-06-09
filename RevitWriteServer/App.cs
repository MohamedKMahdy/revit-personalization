using Autodesk.Revit.UI;
using RevitWriteServer.Chat;
using RevitWriteServer.Commands;

namespace RevitWriteServer;

/// <summary>
/// IExternalApplication entry point.
///
/// On startup:
///  1. Registers the WebView2 dockable pane ("BIM Assistant") in OnStartup.
///  2. Adds a "BIM Assistant" ribbon tab with an "Open BIM Assistant" button.
///  3. Defers TCP server start to ApplicationInitialized (needs UIApplication).
///
/// When a pattern is detected, NotifyPatternCommand shows the dockable pane
/// and navigates it to the Python chatbot server at http://localhost:5000.
/// The Python server is auto-started from chatbot/chat_server.py if not running.
/// </summary>
public class App : IExternalApplication
{
    private TcpCommandServer? _server;

    public Result OnStartup(UIControlledApplication application)
    {
        try
        {
            // ── 1. Register the WebView2 dockable pane ────────────────────────
            var paneProvider = new WebChatPaneProvider();
            application.RegisterDockablePane(
                WebChatPaneProvider.PanelId,
                "BIM Assistant",
                paneProvider);

            // ── 2. Ribbon tab ─────────────────────────────────────────────────
            try { application.CreateRibbonTab("BIM Assistant"); } catch { /* already exists */ }

            var panel = application.CreateRibbonPanel("BIM Assistant", "Pattern Shortcuts");
            var btnData = new PushButtonData(
                "OpenBIMAssistant",
                "Open BIM\nAssistant",
                typeof(App).Assembly.Location,
                typeof(OpenBIMAssistantCommand).FullName!);
            btnData.ToolTip =
                "Show the BIM Assistant panel inside Revit.\n" +
                "The Python chatbot server (localhost:5000) starts automatically.";
            btnData.LongDescription =
                "When a repeated modeling routine is detected, the BIM Assistant " +
                "panel opens here so you can review and run it as a one-click shortcut.";
            panel.AddItem(btnData);

            // ── 3. Defer TCP server to ApplicationInitialized ─────────────────
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

            // Store the DockablePane handle so NotifyPatternCommand and the ribbon
            // button can call Show() without holding a UIApplication reference.
            try
            {
                WebChatPaneProvider.Pane = app.GetDockablePane(WebChatPaneProvider.PanelId);
            }
            catch { /* not critical — pane will still work, just can't auto-show */ }

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
        var sayHello       = new SayHelloCommand(uiApp);
        var getFamilyTypes = new GetFamilyTypesCommand(uiApp);
        var getViewInfo    = new GetViewInfoCommand(uiApp);
        var getSelected    = new GetSelectedElementsCommand(uiApp);
        var placeElement   = new PlaceElementCommand(uiApp);
        var setParameter   = new SetParameterCommand(uiApp);
        var tagElement     = new TagElementCommand(uiApp);
        var notifyPattern  = new NotifyPatternCommand(uiApp);

        // Register all commands with the TCP server
        _server = new TcpCommandServer();
        _server.RegisterCommand(sayHello);
        _server.RegisterCommand(getFamilyTypes);
        _server.RegisterCommand(getViewInfo);
        _server.RegisterCommand(getSelected);
        _server.RegisterCommand(placeElement);
        _server.RegisterCommand(setParameter);
        _server.RegisterCommand(tagElement);
        _server.RegisterCommand(notifyPattern);

        _server.Start();

        uiApp.Application.WriteJournalComment(
            "RevitWriteServer: TCP server localhost:8080 | BIM Assistant pane + localhost:5000",
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
