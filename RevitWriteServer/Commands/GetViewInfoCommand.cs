using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Returns metadata about the currently active view.
/// The Macro Agent calls this to verify the user is on a floor-plan view before
/// attempting any placement operations.
///
/// Response shape:
///   { "view": { "id": 56789, "name": "Level 1", "type": "FloorPlan", "level": "Level 1" } }
/// </summary>
public class GetViewInfoCommand : CommandBase<ViewInfo>
{
    public override string CommandName => "get_current_view_info";

    public GetViewInfoCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters) { }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        var view = doc.ActiveView;
        if (view == null)
        {
            Result = new ViewInfo { Name = "<none>", Type = "None" };
            return;
        }

        var levelName = "";
        if (view is ViewPlan vp)
        {
            var level = doc.GetElement(vp.GenLevel.Id) as Level;
            levelName = level?.Name ?? "";
        }

        Result = new ViewInfo
        {
            Id = view.Id.Value,
            Name = view.Name,
            Type = view.ViewType.ToString(),
            Level = levelName
        };
    }

    protected override object GetResult() => new { view = Result };
}

public record ViewInfo
{
    public long Id { get; init; }
    public string Name { get; init; } = "";
    public string Type { get; init; } = "";
    public string Level { get; init; } = "";
}
