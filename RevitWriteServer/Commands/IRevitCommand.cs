using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Every write-server command implements this interface.
/// Execute() is called from the TCP background thread; implementations
/// must marshal to the Revit UI thread via ExternalEvent (see CommandBase).
/// </summary>
public interface IRevitCommand
{
    string CommandName { get; }
    object Execute(JsonNode? parameters, string requestId);
}
