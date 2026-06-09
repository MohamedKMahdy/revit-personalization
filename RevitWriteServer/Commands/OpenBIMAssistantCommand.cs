using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using RevitWriteServer.Chat;

namespace RevitWriteServer.Commands;

/// <summary>
/// Ribbon button command — shows the BIM Assistant dockable pane and ensures
/// the Python chatbot server (localhost:5000) is running.
///
/// Clicking the button:
///   1. Shows / focuses the WebView2 dockable pane inside Revit.
///   2. Starts chatbot/chat_server.py if not already running.
///   3. Navigates the pane to http://localhost:5000.
///
/// Falls back to opening the system browser if WebView2 is unavailable.
/// </summary>
[Transaction(TransactionMode.Manual)]
public class OpenBIMAssistantCommand : IExternalCommand
{
    public Result Execute(
        ExternalCommandData commandData,
        ref string          message,
        ElementSet          elements)
    {
        // commandData.Application is always valid here — no stored reference needed
        try
        {
            commandData.Application
                .GetDockablePane(WebChatPaneProvider.PanelId)
                .Show();
        }
        catch (Exception ex)
        {
            message = $"Could not show BIM Assistant pane: {ex.Message}";
        }

        // Start server + navigate WebView2 on a background thread
        _ = Task.Run(() => ChatbotLauncher.OpenAsync());

        return Result.Succeeded;
    }
}
