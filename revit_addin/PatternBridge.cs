using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;

namespace RevitLogger;

/// <summary>
/// Sends a notify_pattern JSON-RPC call to RevitWriteServer on localhost:8080.
///
/// Converts a RoutineDetectedEventArgs into the exact payload that
/// RevitWriteServer's NotifyPatternCommand expects — no Python needed.
///
/// IMPORTANT: Always call this from a background thread (Task.Run).
/// The TcpCommandServer in RevitWriteServer dispatches the command via ExternalEvent,
/// which blocks until the Revit UI thread runs the handler. If PatternBridge were
/// called from the UI thread, it would deadlock (UI thread waiting for TCP response
/// while the ExternalEvent waits for the UI thread).
///
/// On success: RevitWriteServer shows the dockable BIM Assistant panel and starts
/// the Claude greeting — no user action required.
/// </summary>
public static class PatternBridge
{
    private const string Host    = "localhost";
    private const int    Port    = 8080;
    private const int    TimeoutMs = 5_000;   // connect + full round-trip timeout

    // ── Entry point ───────────────────────────────────────────────────────────

    public static async Task NotifyAsync(RoutineDetectedEventArgs args)
    {
        try
        {
            App.DiagLog($"PatternBridge: preparing '{args.Label}' ({args.Count}×)");

            var motif = BuildMotif(args.LatestEpisode);
            var seq   = BuildToolSequence(args.LatestEpisode);

            var payload = new JsonObject
            {
                ["jsonrpc"] = "2.0",
                ["method"]  = "notify_pattern",
                ["id"]      = "pb_" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ["params"]  = new JsonObject
                {
                    ["label"]         = args.Label,
                    ["count"]         = args.Count,
                    ["motif"]         = motif,
                    ["tool_sequence"] = seq,
                }
            };

            var json = payload.ToJsonString();
            App.DiagLog($"PatternBridge: payload ({json.Length} chars): {json[..Math.Min(200, json.Length)]}…");

            // Connect with timeout (RevitWriteServer might not be running)
            using var timeoutCts = new CancellationTokenSource(TimeoutMs);
            using var client     = new TcpClient();

            await client.ConnectAsync(Host, Port, timeoutCts.Token);
            using var stream = client.GetStream();

            // Send the JSON request
            var bytes = Encoding.UTF8.GetBytes(json);
            await stream.WriteAsync(bytes, timeoutCts.Token);
            await stream.FlushAsync(timeoutCts.Token);

            // Read response — blocks until the ExternalEvent completes and
            // RevitWriteServer closes the connection.  We discard the bytes;
            // we just need the synchronisation guarantee.
            var buf = new byte[4096];
            while (await stream.ReadAsync(buf, timeoutCts.Token) > 0) { /* discard */ }

            App.DiagLog($"PatternBridge: done — BIM Assistant panel should now be active");
        }
        catch (OperationCanceledException)
        {
            App.DiagLog($"PatternBridge: timeout ({TimeoutMs} ms) — RevitWriteServer not running or busy");
        }
        catch (SocketException sex)
        {
            App.DiagLog($"PatternBridge: socket error — RevitWriteServer not loaded? ({sex.SocketErrorCode}: {sex.Message})");
        }
        catch (Exception ex)
        {
            App.DiagLog($"PatternBridge ERROR: {ex.GetType().Name}: {ex.Message}");
        }
    }

    // ── Build motif ───────────────────────────────────────────────────────────
    //
    // Produces:
    //   { "steps": [ {"action":"Place","family_type":"..."},
    //                {"action":"SetParam","param_name":"...","param_value":"..."},
    //                {"action":"Tag","family_type":"..."}  ] }

    private static JsonObject BuildMotif(List<ActionRecord> episode)
    {
        var steps = new JsonArray();
        foreach (var a in episode)
        {
            JsonObject step = a.ActionType switch
            {
                "Place" => new JsonObject
                {
                    ["action"]      = "Place",
                    ["family_type"] = FamilyTypeLabel(a),
                },
                "SetParam" => new JsonObject
                {
                    ["action"]      = "SetParam",
                    ["param_name"]  = a.ParamName,
                    ["param_value"] = a.ParamValueAfter?.ToString(),
                },
                "Tag" => new JsonObject
                {
                    ["action"]      = "Tag",
                    ["family_type"] = a.TagFamilyName,
                },
                _ => new JsonObject { ["action"] = a.ActionType },
            };
            steps.Add(step);
        }
        return new JsonObject { ["steps"] = steps };
    }

    // ── Build tool_sequence ───────────────────────────────────────────────────
    //
    // Produces a flat array of MCP tool-call steps:
    //   [
    //     {"tool":"place_element",         "arguments":{...}},
    //     {"tool":"set_parameter",         "arguments":{"element_id":"{{last_element_id}}",...}},
    //     {"tool":"create_annotation_tag", "arguments":{"element_id":"{{last_element_id}}",...}}
    //   ]
    //
    // {{last_element_id}} is a placeholder resolved by NotifyPatternCommand.ExecuteToolSequence
    // to the ElementId returned by the most recent place_element call.

    private static JsonArray BuildToolSequence(List<ActionRecord> episode)
    {
        var steps  = new JsonArray();
        bool placed = false;

        foreach (var a in episode)
        {
            switch (a.ActionType)
            {
                case "Place":
                    steps.Add(new JsonObject
                    {
                        ["tool"] = "place_element",
                        ["arguments"] = new JsonObject
                        {
                            ["family_type"] = FamilyTypeLabel(a),
                            // x/y/z are 0,0,0 — the shortcut places at the cursor position
                            // when executed; the location is set interactively in Revit.
                            ["location"] = new JsonObject
                            {
                                ["x"] = 0.0,
                                ["y"] = 0.0,
                                ["z"] = 0.0,
                            },
                        }
                    });
                    placed = true;
                    break;

                case "SetParam":
                    steps.Add(new JsonObject
                    {
                        ["tool"] = "set_parameter",
                        ["arguments"] = new JsonObject
                        {
                            // Use placeholder if we've placed an element; otherwise fall
                            // back to the recorded element_id (shouldn't happen in practice).
                            ["element_id"]     = placed
                                                    ? JsonValue.Create("{{last_element_id}}")
                                                    : JsonValue.Create(a.ElementId),
                            ["parameter_name"] = a.ParamName,
                            ["value"]          = a.ParamValueAfter?.ToString(),
                        }
                    });
                    break;

                case "Tag":
                    steps.Add(new JsonObject
                    {
                        ["tool"] = "create_annotation_tag",
                        ["arguments"] = new JsonObject
                        {
                            ["element_id"] = placed
                                                ? JsonValue.Create("{{last_element_id}}")
                                                : JsonValue.Create(a.ElementId),
                            ["tag_family"] = a.TagFamilyName,
                        }
                    });
                    break;
            }
        }

        return steps;
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    /// <summary>
    /// Returns "FamilyName : TypeName" when TypeName is set, else just "FamilyName".
    /// Matches the format expected by NotifyPatternCommand.ResolveFamilyTypeId.
    /// </summary>
    private static string FamilyTypeLabel(ActionRecord a)
        => string.IsNullOrEmpty(a.TypeName)
            ? a.FamilyName
            : $"{a.FamilyName} : {a.TypeName}";
}
