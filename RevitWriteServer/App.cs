using Autodesk.Revit.UI;
using RevitWriteServer.Commands;

namespace RevitWriteServer;

/// <summary>
/// IExternalApplication entry point for the RevitWriteServer add-in.
///
/// On startup:
///  1. Creates all command handlers (each registers an ExternalEvent).
///  2. Starts the TcpCommandServer on localhost:8080.
///
/// The TCP server runs on a background thread for the entire Revit session.
/// The Python bridge (revit_bridge.py) connects to it to execute model writes.
///
/// Architecture:
///   Python revit_bridge.py
///       ↕ TCP JSON-RPC localhost:8080
///   RevitWriteServer (this add-in)
///       ├── TcpCommandServer  — listens, parses JSON-RPC, dispatches
///       └── IRevitCommand impls — marshal to Revit UI thread via ExternalEvent
/// </summary>
public class App : IExternalApplication
{
    private TcpCommandServer? _server;

    public Result OnStartup(UIControlledApplication application)
    {
        try
        {
            // We need a UIApplication to create ExternalEvents.
            // In IExternalApplication.OnStartup, we only have UIControlledApplication.
            // We defer server creation until the ApplicationInitialized event fires,
            // at which point we have access to UIApplication.
            application.ControlledApplication.ApplicationInitialized += OnApplicationInitialized;
            return Result.Succeeded;
        }
        catch (Exception ex)
        {
            ShowError("RevitWriteServer startup failed", ex);
            return Result.Failed;
        }
    }

    private void OnApplicationInitialized(object? sender, Autodesk.Revit.DB.Events.ApplicationInitializedEventArgs e)
    {
        try
        {
            // Obtain the UIApplication from the sender (it's the Application object).
            // We need UIApplication to construct ExternalEvents.
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
        _server = new TcpCommandServer();

        // Register all commands — each command creates its own ExternalEvent on construction
        _server.RegisterCommand(new SayHelloCommand(uiApp));
        _server.RegisterCommand(new GetFamilyTypesCommand(uiApp));
        _server.RegisterCommand(new GetViewInfoCommand(uiApp));
        _server.RegisterCommand(new GetSelectedElementsCommand(uiApp));
        _server.RegisterCommand(new PlaceElementCommand(uiApp));
        _server.RegisterCommand(new SetParameterCommand(uiApp));   // ← NEW
        _server.RegisterCommand(new TagElementCommand(uiApp));     // ← NEW

        _server.Start();

        // Brief confirmation in the status bar (non-modal)
        uiApp.Application.WriteJournalComment(
            "RevitWriteServer: TCP server started on localhost:8080", true);
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

    private static void ShowError(string title, Exception ex)
    {
        TaskDialog.Show(title, $"{ex.GetType().Name}: {ex.Message}");
    }
}
