using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace RevitLogger;

/// <summary>
/// Keeps the "Open Assistant" ribbon button enabled even when no document is open.
/// Revit grays out external-command buttons with no active document by default; the
/// assistant pane is a pure WebView2 chat host that never touches the model, so it
/// must stay clickable from the Revit Home/start screen too.
/// </summary>
public class AssistantAvailability : IExternalCommandAvailability
{
    public bool IsCommandAvailable(UIApplication applicationData, CategorySet selectedCategories) => true;
}
