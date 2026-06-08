using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Returns all loaded family types in the document, with their ElementId and category.
/// The Python bridge calls this first to resolve a family-type name to a numeric TypeId
/// before calling create_point_based_element.
///
/// Response shape:
///   { "types": [ { "id": 12345, "name": "M_Single-Flush : 900x2100mm", "category": "Doors" }, … ] }
/// </summary>
public class GetFamilyTypesCommand : CommandBase<List<FamilyTypeInfo>>
{
    public override string CommandName => "get_available_family_types";

    // Optional filter: if non-null, only return types whose category name contains this string.
    private string? _categoryFilter;

    public GetFamilyTypesCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters)
    {
        _categoryFilter = parameters?["category"]?.GetValue<string>();
    }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        var collector = new FilteredElementCollector(doc)
            .OfClass(typeof(FamilySymbol))
            .Cast<FamilySymbol>();

        var types = new List<FamilyTypeInfo>();
        foreach (var sym in collector)
        {
            var catName = sym.Category?.Name ?? "Unknown";
            if (_categoryFilter != null &&
                !catName.Contains(_categoryFilter, StringComparison.OrdinalIgnoreCase))
                continue;

            types.Add(new FamilyTypeInfo
            {
                Id = sym.Id.Value,
                Name = $"{sym.FamilyName} : {sym.Name}",
                Category = catName
            });
        }

        types.Sort((a, b) => string.Compare(a.Name, b.Name, StringComparison.Ordinal));
        Result = types;
    }

    protected override object GetResult() => new { types = Result ?? new List<FamilyTypeInfo>() };
}

public record FamilyTypeInfo
{
    public long Id { get; init; }
    public string Name { get; init; } = "";
    public string Category { get; init; } = "";
}
