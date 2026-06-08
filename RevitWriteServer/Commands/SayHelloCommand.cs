using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Connection test — shows a task-dialog in Revit and returns a status string.
/// Call this first to confirm the plugin is loaded and the TCP socket is reachable.
/// </summary>
public class SayHelloCommand : CommandBase<string>
{
    public override string CommandName => "say_hello";

    public SayHelloCommand(UIApplication uiApp) : base(uiApp) { }

    protected override void PrepareParameters(JsonNode? parameters) { }

    protected override void ExecuteOnRevitThread(Document doc)
    {
        TaskDialog.Show("RevitWriteServer", "Hello from RevitWriteServer (Revit 2027)!");
        Result = $"Hello from Revit {doc.Application.VersionName}";
    }

    protected override object GetResult() => new { status = Result ?? "ok" };
}
