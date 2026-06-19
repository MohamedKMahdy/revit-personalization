using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace BIMAssistant;

/// <summary>
/// Ribbon button command — shows the embedded "BIM Assistant" dockable pane. The pane
/// owns server warm-up and navigation (see AssistantPane), so this only reveals the
/// pane and asks it to (re)load.
/// </summary>
[Transaction(TransactionMode.ReadOnly)]
public class OpenAssistantCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        if (App.AssistantPaneInstance is null)
        {
            message = "BIM Assistant pane failed to register at startup. Restart Revit; "
                    + "if it persists, check %LOCALAPPDATA%\\RevitPersonalization\\logs\\_assistant.txt.";
            return Result.Failed;
        }

        try
        {
            DockablePane pane;
            try
            {
                pane = commandData.Application.GetDockablePane(AssistantPane.PaneId);
            }
            catch (Autodesk.Revit.Exceptions.ArgumentException)
            {
                message = "BIM Assistant pane is not registered.";
                return Result.Failed;
            }

            pane.Show();
            // Fresh load when re-opened (no-op on the first open: the pane's own Loaded
            // handler drives the initial navigation).
            App.AssistantPaneInstance.Reload();
            return Result.Succeeded;
        }
        catch (Exception ex)
        {
            message = $"Failed to open BIM Assistant: {ex.Message}";
            App.DiagLog($"OpenAssistantCommand EXCEPTION: {ex}");
            return Result.Failed;
        }
    }
}
