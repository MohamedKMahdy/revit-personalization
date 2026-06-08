using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Sets a parameter on an element by name or GUID.
/// This is NEW functionality not present in mcp-servers-for-revit v1.0.0,
/// where modify_element.js was an empty stub.
///
/// Required params:
///   elementId     (int)    — ElementId of the target element
///   parameterName (string) — Built-in or shared parameter name (case-insensitive)
///   value         (any)    — String, integer, or double value to set
///
/// Optional params:
///   parameterGuid (string) — GUID of shared parameter (preferred over name if provided)
///
/// Response shape:
///   { "success": true, "elementId": 99887, "parameterName": "Mark", "value": "D-101" }
/// </summary>
public class SetParameterCommand : CommandBase<SetParameterResult>
{
    public override string CommandName => "set_element_parameter";

    private long _elementId;
    private string _parameterName = "";
    private string? _parameterGuid;
    private JsonNode? _value;

    public SetParameterCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters)
    {
        if (parameters == null)
            throw new ArgumentException("Parameters are required for set_element_parameter");

        _elementId = parameters["elementId"]?.GetValue<long>()
                     ?? throw new ArgumentException("Missing required parameter: elementId");
        _parameterName = parameters["parameterName"]?.GetValue<string>()
                         ?? throw new ArgumentException("Missing required parameter: parameterName");
        _parameterGuid = parameters["parameterGuid"]?.GetValue<string>();
        _value = parameters["value"];
    }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        var element = doc.GetElement(new ElementId(_elementId))
                      ?? throw new ArgumentException($"No element found with id {_elementId}");

        // Resolve the parameter
        Parameter? param = null;

        if (_parameterGuid != null && Guid.TryParse(_parameterGuid, out var guid))
        {
            param = element.get_Parameter(guid);
        }

        if (param == null)
        {
            // Try name match (case-insensitive)
            param = element.Parameters
                           .Cast<Parameter>()
                           .FirstOrDefault(p =>
                               string.Equals(p.Definition.Name, _parameterName,
                                             StringComparison.OrdinalIgnoreCase));
        }

        if (param == null)
            throw new ArgumentException($"Parameter '{_parameterName}' not found on element {_elementId}");

        if (param.IsReadOnly)
            throw new InvalidOperationException($"Parameter '{_parameterName}' is read-only");

        using var tx = new Transaction(doc, $"RevitWriteServer: SetParameter '{_parameterName}'");
        tx.Start();

        SetParameterValue(param, _value);

        tx.Commit();

        var storedValue = GetParameterValueString(param);
        Result = new SetParameterResult
        {
            Success = true,
            ElementId = _elementId,
            ParameterName = param.Definition.Name,
            Value = storedValue
        };
    }

    private static void SetParameterValue(Parameter param, JsonNode? value)
    {
        if (value == null) return;

        switch (param.StorageType)
        {
            case StorageType.String:
                param.Set(value.GetValue<string>());
                break;

            case StorageType.Integer:
                // Accept "1" or 1
                if (value is JsonValue jv && jv.TryGetValue<int>(out var intVal))
                    param.Set(intVal);
                else
                    param.Set(int.Parse(value.GetValue<string>()));
                break;

            case StorageType.Double:
                // The value coming from Python is in display units; convert if needed.
                // For simplicity, accept internal units directly. The Python bridge
                // is responsible for unit conversion (feet for lengths, etc.)
                if (value is JsonValue jd && jd.TryGetValue<double>(out var dblVal))
                    param.Set(dblVal);
                else
                    param.Set(double.Parse(value.GetValue<string>()));
                break;

            case StorageType.ElementId:
                param.Set(new ElementId(value.GetValue<long>()));
                break;

            default:
                throw new NotSupportedException($"StorageType {param.StorageType} is not supported");
        }
    }

    private static string GetParameterValueString(Parameter param) =>
        param.StorageType switch
        {
            StorageType.String  => param.AsString() ?? "",
            StorageType.Integer => param.AsInteger().ToString(),
            StorageType.Double  => param.AsDouble().ToString("G"),
            StorageType.ElementId => param.AsElementId()?.Value.ToString() ?? "-1",
            _ => ""
        };

    protected override object GetResult() => Result ?? throw new InvalidOperationException("Result not set");
}

public record SetParameterResult
{
    public bool Success { get; init; }
    public long ElementId { get; init; }
    public string ParameterName { get; init; } = "";
    public string Value { get; init; } = "";
}
