using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace BIMAssistant;

/// <summary>
/// Enables the "Open Assistant" button only when a project is open. A Revit dockable pane
/// cannot be shown on the Home/start screen (no active document) — GetDockablePane/Show throw
/// there — so leaving the button clickable produced a misleading "pane is not registered" error.
/// Greying it out with no document open is the honest UX: open a project, then the button enables.
/// </summary>
public class AssistantAvailability : IExternalCommandAvailability
{
    public bool IsCommandAvailable(UIApplication applicationData, CategorySet selectedCategories)
        => applicationData?.ActiveUIDocument != null;
}
