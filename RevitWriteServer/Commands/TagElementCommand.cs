using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Creates an annotation tag for an existing element.
/// This is NEW functionality not present in mcp-servers-for-revit v1.0.0.
///
/// Required params:
///   elementId  (int)    — ElementId of the element to tag
///
/// Optional params:
///   tagTypeId  (int)    — ElementId of the tag FamilySymbol to use.
///                         If omitted, uses the first available tag for the element's category.
///   addLeader  (bool)   — Whether to add a leader line (default: false)
///   offsetX    (double) — Tag head X offset from element origin in feet (default: 1.0)
///   offsetY    (double) — Tag head Y offset from element origin in feet (default: 1.0)
///
/// Response shape:
///   { "tagId": 112233, "elementId": 99887, "tagTypeName": "Door Tag" }
/// </summary>
public class TagElementCommand : CommandBase<TagElementResult>
{
    public override string CommandName => "create_element_tag";

    private long _elementId;
    private long? _tagTypeId;
    private bool _addLeader;
    private double _offsetX = 1.0;
    private double _offsetY = 1.0;

    public TagElementCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters)
    {
        if (parameters == null)
            throw new ArgumentException("Parameters are required for create_element_tag");

        _elementId = parameters["elementId"]?.GetValue<long>()
                     ?? throw new ArgumentException("Missing required parameter: elementId");
        _tagTypeId = parameters["tagTypeId"]?.GetValue<long?>();
        _addLeader = parameters["addLeader"]?.GetValue<bool>() ?? false;
        _offsetX   = parameters["offsetX"]?.GetValue<double>() ?? 1.0;
        _offsetY   = parameters["offsetY"]?.GetValue<double>() ?? 1.0;
    }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        var element = doc.GetElement(new ElementId(_elementId))
                      ?? throw new ArgumentException($"No element found with id {_elementId}");

        var activeView = doc.ActiveView;
        if (activeView == null)
            throw new InvalidOperationException("No active view to place tag in");

        // Resolve tag type
        FamilySymbol? tagSymbol = null;

        if (_tagTypeId.HasValue)
        {
            tagSymbol = doc.GetElement(new ElementId(_tagTypeId.Value)) as FamilySymbol;
            if (tagSymbol == null)
                throw new ArgumentException($"No FamilySymbol found with id {_tagTypeId}");
        }
        else
        {
            // Find a tag type that matches the element's category
            var elementCategoryId = element.Category?.Id;
            tagSymbol = FindTagTypeForCategory(doc, elementCategoryId);
            if (tagSymbol == null)
                throw new InvalidOperationException(
                    $"No tag type found for category '{element.Category?.Name ?? "Unknown"}'. " +
                    "Provide tagTypeId explicitly.");
        }

        if (!tagSymbol.IsActive)
            tagSymbol.Activate();

        // Compute tag head position (offset from element origin)
        var origin = GetElementOrigin(element);
        var tagPoint = new XYZ(origin.X + _offsetX, origin.Y + _offsetY, origin.Z);

        using var tx = new Transaction(doc, "RevitWriteServer: CreateTag");
        tx.Start();

        var tag = IndependentTag.Create(
            doc,
            tagSymbol.Id,
            activeView.Id,
            new Reference(element),
            _addLeader,
            TagOrientation.Horizontal,
            tagPoint);

        tx.Commit();

        Result = new TagElementResult
        {
            TagId = tag.Id.Value,
            ElementId = _elementId,
            TagTypeName = $"{tagSymbol.FamilyName} : {tagSymbol.Name}"
        };
    }

    private static FamilySymbol? FindTagTypeForCategory(Document doc, ElementId? categoryId)
    {
        if (categoryId == null) return null;

        // Tag families are in the "Tags" category group.
        // We look for FamilySymbols whose OwnerFamily category is a tag category
        // matching the target element category.
        return new FilteredElementCollector(doc)
            .OfClass(typeof(FamilySymbol))
            .Cast<FamilySymbol>()
            .FirstOrDefault(sym =>
            {
                var family = sym.Family;
                // A tag family's FamilyCategory targets the category it annotates
                return family?.FamilyCategory?.Id == categoryId;
            });
    }

    private static XYZ GetElementOrigin(Element element)
    {
        if (element.Location is LocationPoint lp) return lp.Point;
        if (element.Location is LocationCurve lc) return lc.Curve.Evaluate(0.5, true);
        return XYZ.Zero;
    }

    protected override object GetResult() => Result ?? throw new InvalidOperationException("Result not set");
}

public record TagElementResult
{
    public long TagId { get; init; }
    public long ElementId { get; init; }
    public string TagTypeName { get; init; } = "";
}
