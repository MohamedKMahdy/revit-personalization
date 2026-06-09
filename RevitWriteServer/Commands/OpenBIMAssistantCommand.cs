using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using RevitWriteServer.Chat;

namespace RevitWriteServer.Commands;

/// <summary>
/// Ribbon button command — opens the BIM Assistant browser UI.
///
/// If the Python chatbot server (localhost:5000) is not already running,
/// it is started automatically from chatbot/chat_server.py before the
/// browser is opened.
///
/// Added to the "BIM Assistant" ribbon tab by App.OnStartup.
/// No Revit transaction needed — just process + browser management.
/// </summary>
[Transaction(TransactionMode.Manual)]
public class OpenBIMAssistantCommand : IExternalCommand
{
    public Result Execute(
        ExternalCommandData commandData,
        ref string          message,
        ElementSet          elements)
    {
        // Fire-and-forget: network I/O must not block the Revit UI thread
        _ = Task.Run(() => ChatbotLauncher.OpenAsync());
        return Result.Succeeded;
    }
}
