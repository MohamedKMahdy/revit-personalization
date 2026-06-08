using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Places a point-based family instance at the given XYZ coordinates.
/// This mirrors the "create_point_based_element" tool from mcp-servers-for-revit v1.0.0,
/// with the same parameter contract so the Python bridge works against both backends.
///
/// Required params:
///   typeId  (int)    — ElementId of the FamilySymbol (get from get_available_family_types)
///   x, y, z (double) — coordinates in decimal feet (Revit internal units)
///
/// Optional params:
///   levelId (int)    — ElementId of the level; defaults to the first level found
///
/// Response shape:
///   { "elementId": 99887, "x": 0.0, "y": 0.0, "z": 0.0 }
/// </summary>
public class PlaceElementCommand : CommandBase<PlaceElementResult>
{
    public override string CommandName => "create_point_based_element";

    private long _typeId;
    private double _x, _y, _z;
    private long? _levelId;

    public PlaceElementCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters)
    {
        if (parameters == null)
            throw new ArgumentException("Parameters are required for create_point_based_element");

        _typeId  = parameters["typeId"]?.GetValue<long>()
                   ?? throw new ArgumentException("Missing required parameter: typeId");
        _x = parameters["x"]?.GetValue<double>() ?? 0.0;
        _y = parameters["y"]?.GetValue<double>() ?? 0.0;
        _z = parameters["z"]?.GetValue<double>() ?? 0.0;
        _levelId = parameters["levelId"]?.GetValue<long?>();
    }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        // Resolve the FamilySymbol
        var symbolId = new ElementId(_typeId);
        if (doc.GetElement(symbolId) is not FamilySymbol symbol)
            throw new ArgumentException($"No FamilySymbol found with id {_typeId}");

        // Activate the symbol if needed (required before first placement)
        if (!symbol.IsActive)
            symbol.Activate();

        // Resolve level
        Level? level = null;
        if (_levelId.HasValue)
        {
            level = doc.GetElement(new ElementId(_levelId.Value)) as Level;
        }
        else
        {
            // Use the first level in the document
            level = new FilteredElementCollector(doc)
                .OfClass(typeof(Level))
                .Cast<Level>()
                .OrderBy(l => l.Elevation)
                .FirstOrDefault();
        }

        var point = new XYZ(_x, _y, _z);

        using var tx = new Transaction(doc, "RevitWriteServer: PlaceElement");
        tx.Start();

        FamilyInstance? instance;
        if (level != null)
            instance = doc.Create.NewFamilyInstance(point, symbol, level, StructuralType.NonStructural);
        else
            instance = doc.Create.NewFamilyInstance(point, symbol, StructuralType.NonStructural);

        tx.Commit();

        var loc = (instance.Location as LocationPoint)?.Point ?? point;
        Result = new PlaceElementResult
        {
            ElementId = instance.Id.Value,
            X = loc.X,
            Y = loc.Y,
            Z = loc.Z
        };
    }

    protected override object GetResult() => Result ?? throw new InvalidOperationException("Result not set");
}

public record PlaceElementResult
{
    public long ElementId { get; init; }
    public double X { get; init; }
    public double Y { get; init; }
    public double Z { get; init; }
}
