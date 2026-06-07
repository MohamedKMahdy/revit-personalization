using System;
using System.Collections.Generic;
using System.Linq;

namespace RevitLogger;

/// <summary>
/// Real-time detector for Custom Element Instantiation (CEI) routines.
///
/// Watches the live action stream from ActionCapture and fires RoutineDetected
/// when the user has repeated the same Place → SetParam* → Tag? episode pattern
/// two or more times within the session.
///
/// Detection algorithm (mirrors mcp_server/log_reader.py for ID compatibility):
///   1. Group actions by element_id — each element's sequence is one episode.
///   2. An episode is "complete" when:
///        (a) a Tag action arrives whose tagged_element_id matches the episode, OR
///        (b) a new Place is received for the same element_id (cleanup path).
///   3. Compute structural signature: "{category}|{family}|Place,SetParam(P1),Tag"
///   4. When ≥ 2 episodes share the same signature → fire RoutineDetected.
///
/// ID format is intentionally identical to log_reader.py so the Python orchestrator
/// can look up the routine in the MCP server's candidate_routines resource.
///
/// Thread safety: Feed() is driven by ActionCapture.ProcessEvent(), which Revit
/// calls on the UI thread (DocumentChanged). All state is single-threaded.
/// RoutineDetected fires on the UI thread — safe to show WPF windows directly.
/// </summary>
public class RoutineDetector
{
    // element_id → in-progress episode (Place received, Tag not yet seen)
    private readonly Dictionary<int, List<ActionRecord>> _activeEpisodes = new();

    // structural signature → completed episodes with that signature
    private readonly Dictionary<string, List<CompletedEpisode>> _completedBySignature = new();

    private const int MinRepetitions = 2;

    /// <summary>
    /// Fired on the Revit UI thread when MinRepetitions identical episodes are confirmed.
    /// </summary>
    public event EventHandler<RoutineDetectedEventArgs>? RoutineDetected;

    // ── Feed ─────────────────────────────────────────────────────────────────

    /// <summary>
    /// Called after every ActionRecord is enqueued to the log writer.
    /// Must be called on the Revit UI thread.
    /// </summary>
    public void Feed(ActionRecord record)
    {
        var elementId = record.ElementId;

        switch (record.ActionType)
        {
            case "Place":
                // If there is a stale open episode for this element (e.g. user never tagged
                // the previous placement), try to finalize it before starting the new one.
                if (_activeEpisodes.TryGetValue(elementId, out var stale) && stale.Count >= 2)
                    FinalizeEpisode(elementId, stale);

                _activeEpisodes[elementId] = new List<ActionRecord> { record };
                break;

            case "SetParam":
                if (_activeEpisodes.TryGetValue(elementId, out var ep))
                    ep.Add(record);
                break;

            case "Tag":
                // Tag records use tagged_element_id to identify which element was tagged.
                // ActionCapture.EmitTag sets TaggedElementId from GetTaggedReferences().
                var targetId = record.TaggedElementId ?? elementId;
                if (_activeEpisodes.TryGetValue(targetId, out var taggedEp))
                {
                    taggedEp.Add(record);
                    FinalizeEpisode(targetId, taggedEp);
                    _activeEpisodes.Remove(targetId);
                }
                break;
        }
    }

    // ── Finalization ──────────────────────────────────────────────────────────

    private void FinalizeEpisode(int elementId, List<ActionRecord> actions)
    {
        // Require at least Place + 1 additional action to form a meaningful episode
        if (actions.Count < 2) return;
        // Only consider episodes that begin with a Place (consistent with log_reader.py)
        if (actions[0].ActionType != "Place") return;

        var sig = ComputeSignature(actions);

        if (!_completedBySignature.TryGetValue(sig, out var episodes))
        {
            episodes = new List<CompletedEpisode>();
            _completedBySignature[sig] = episodes;
        }

        // Store a snapshot of the episode (copy so the list can be safely reused)
        episodes.Add(new CompletedEpisode(elementId, actions.ToList()));

        if (episodes.Count >= MinRepetitions)
        {
            App.DiagLog($"RoutineDetector: confirmed sig='{sig}' count={episodes.Count}");
            RoutineDetected?.Invoke(this, new RoutineDetectedEventArgs(
                id:            BuildRoutineId(sig),
                label:         BuildLabel(actions),
                count:         episodes.Count,
                signature:     sig,
                latestEpisode: actions.ToList()
            ));
        }
    }

    // ── Signature + ID (match log_reader.py exactly) ──────────────────────────

    /// <summary>
    /// Full structural signature. Format matches log_reader.py._episode_signature().
    /// Example: "Doors|Door-Passage-Single-Full_Lite|Place,SetParam(Mark),Tag"
    /// </summary>
    private static string ComputeSignature(List<ActionRecord> actions)
    {
        var parts = actions.Select(a => a.ActionType switch
        {
            "Place"    => "Place",
            "SetParam" => $"SetParam({a.ParamName ?? ""})",
            "Tag"      => "Tag",
            _          => a.ActionType,
        });

        var first  = actions[0];
        var cat    = first.ElementCategory;
        var family = first.FamilyName.Split(':')[0].Trim();   // match Python .split(":")[0]
        return $"{cat}|{family}|{string.Join(",", parts)}";
    }

    /// <summary>
    /// Routine ID that matches log_reader.py's generation:
    ///   "routine_" + sig.replace("|","_").replace(",","_")
    ///                   .replace("(","").replace(")","")
    ///                   .replace(" ","")[:40]
    /// This allows the orchestrator to look up the routine by ID in the MCP server.
    /// </summary>
    private static string BuildRoutineId(string sig)
    {
        var transformed = sig
            .Replace("|", "_")
            .Replace(",", "_")
            .Replace("(", "")
            .Replace(")", "")
            .Replace(" ", "");
        if (transformed.Length > 40)
            transformed = transformed[..40];
        return "routine_" + transformed;
    }

    /// <summary>
    /// Human-readable label. Format matches log_reader.py._build_label().
    /// Example: "Place(Door-Passage-Single-Full_Lite) → SetParam×2 → Tag"
    /// </summary>
    private static string BuildLabel(List<ActionRecord> actions)
    {
        var parts     = new List<string>();
        int setCount  = 0;
        foreach (var a in actions)
        {
            switch (a.ActionType)
            {
                case "Place":
                    var fname = a.FamilyName.Split(':')[0].Trim();
                    if (string.IsNullOrEmpty(fname)) fname = a.ElementCategory;
                    parts.Add($"Place({fname})");
                    break;
                case "SetParam":
                    setCount++;
                    break;
                case "Tag":
                    if (setCount > 0) { parts.Add($"SetParam×{setCount}"); setCount = 0; }
                    parts.Add($"Tag({a.TagFamilyName ?? ""})");
                    break;
            }
        }
        if (setCount > 0) parts.Add($"SetParam×{setCount}");
        return string.Join(" → ", parts);
    }
}

// ── Supporting types ──────────────────────────────────────────────────────────

internal record CompletedEpisode(int ElementId, List<ActionRecord> Actions);

public sealed class RoutineDetectedEventArgs : EventArgs
{
    public string             Id            { get; }
    public string             Label         { get; }
    public int                Count         { get; }
    public string             Signature     { get; }
    public List<ActionRecord> LatestEpisode { get; }

    internal RoutineDetectedEventArgs(string id, string label, int count,
                                      string signature, List<ActionRecord> latestEpisode)
    {
        Id            = id;
        Label         = label;
        Count         = count;
        Signature     = signature;
        LatestEpisode = latestEpisode;
    }
}
