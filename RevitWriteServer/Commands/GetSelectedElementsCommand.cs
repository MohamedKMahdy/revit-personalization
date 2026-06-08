using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Returns the currently selected elements in the active view.
/// Useful for the Python bridge to confirm that a just-placed element was created
/// and can also be used to retrieve parameter values for verification.
///
/// Response shape:
///   { "elements": [ { "id": 12345, "category": "Doors", "typeName": "M_Single-Flush : 900x2100mm" }, … ] }
/// </summary>
public class GetSelectedElementsCommand : CommandBase<List<SelectedElementInfo>>
{
    public override string CommandName => "get_selected_elements";

    public GetSelectedElementsCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters) { }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        var selection = UiApp.ActiveUIDocument?.Selection;
        var infos = new List<SelectedElementInfo>();

        if (selection == null)
        {
            Result = infos;
            return;
        }

        foreach (var id in selection.GetElementIds())
        {
            var el = doc.GetElement(id);
            if (el == null) continue;

            var typeName = "";
            if (el.GetTypeId() is ElementId typeId && typeId != ElementId.InvalidElementId)
            {
                var typeEl = doc.GetElement(typeId);
                if (typeEl is FamilySymbol sym)
                    typeName = $"{sym.FamilyName} : {sym.Name}";
                else
                    typeName = typeEl?.Name ?? "";
            }

            infos.Add(new SelectedElementInfo
            {
                Id = id.Value,
                Category = el.Category?.Name ?? "Unknown",
                TypeName = typeName
            });
        }

        Result = infos;
    }

    protected override object GetResult() => new { elements = Result ?? new List<SelectedElementInfo>() };
}

public record SelectedElementInfo
{
    public long Id { get; init; }
    public string Category { get; init; } = "";
    public string TypeName { get; init; } = "";
}
