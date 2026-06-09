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
        // Show the pane on the Revit API thread
        try { WebChatPaneProvider.Pane?.Show(); } catch { }

        // Start server + navigate on a background thread
        _ = Task.Run(() => ChatbotLauncher.OpenAsync());

        return Result.Succeeded;
    }
}
