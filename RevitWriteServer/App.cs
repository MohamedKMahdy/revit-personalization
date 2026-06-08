using Autodesk.Revit.UI;
using RevitWriteServer.Chat;
using RevitWriteServer.Commands;

namespace RevitWriteServer;

/// <summary>
/// IExternalApplication entry point.
///
/// On startup:
///  1. Registers the BIM Assistant dockable pane (chat panel).
///  2. Defers TCP server start to ApplicationInitialized (when UIApplication is available).
///
/// The TCP server accepts JSON-RPC 2.0 commands from the Python orchestrator.
/// The dockable chat panel streams Claude responses directly inside Revit.
/// </summary>
public class App : IExternalApplication
{
    private TcpCommandServer?  _server;
    private ChatPaneProvider?  _paneProvider;

    public Result OnStartup(UIControlledApplication application)
    {
        try
        {
            // Register the dockable pane (must happen in OnStartup)
            _paneProvider = new ChatPaneProvider();
            application.RegisterDockablePane(
                ChatPaneProvider.PanelId,
                "BIM Assistant",
                _paneProvider);

            // Defer TCP server to ApplicationInitialized (we need UIApplication)
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

        // Wire up static refs so NotifyPatternCommand can dispatch directly
        NotifyPatternCommand.PaneProvider    = _paneProvider;
        NotifyPatternCommand.PlaceCmd        = placeElement;
        NotifyPatternCommand.SetParamCmd     = setParameter;
        NotifyPatternCommand.TagCmd          = tagElement;
        NotifyPatternCommand.FamilyTypesCmd  = getFamilyTypes;

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
            "RevitWriteServer: TCP server started on localhost:8080 | BIM Assistant panel registered",
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
