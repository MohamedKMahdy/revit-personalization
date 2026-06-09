using System.Text.Json.Nodes;

namespace RevitWriteServer.Chat;

/// <summary>
/// Carries the detected pattern from NotifyPatternCommand into the WPF panel.
/// ExecuteCallback is a closure that runs the tool sequence directly on the
/// Revit UI thread without needing a TCP round-trip.
/// </summary>
public record PatternData
{
    public string Label { get; init; } = "Detected Routine";
    public int Count { get; init; }
    public JsonNode? Motif { get; init; }
    public JsonArray? ToolSequence { get; init; }
    /// <summary>
    /// Called when the user confirms.
    /// Returns a Task that completes (or faults) when the Revit ExternalEvent finishes.
    /// Awaiting it from the WPF dispatcher yields the message loop so Revit can
    /// dispatch the event — do NOT block-wait (.Result / .Wait()) on the UI thread.
    /// </summary>
    public Func<Task>? ExecuteCallback { get; init; }
}
