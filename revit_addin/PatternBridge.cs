using System.Diagnostics;
using System.IO;
using System.Text.Json.Nodes;

namespace RevitLogger;

/// <summary>
/// Hands a detected routine to the Python BIM Assistant chatbot (http://localhost:5000).
///
/// The add-in is observer-only, so it does NOT host the assistant itself. It writes
/// the pattern to %LOCALAPPDATA%\RevitPersonalization\pending_pattern.json and launches
/// chatbot/notify_from_file.py, which delegates to chatbot.trigger.notify_pattern:
/// that helper starts the chat server if it isn't running, POSTs the pattern to
/// /api/pattern, and opens the browser.
///
/// REPO_ROOT and PYTHON_EXE are read from %LOCALAPPDATA%\RevitPersonalization\.env,
/// written by `python setup_revit_env.py` (the documented one-time setup step).
///
/// IMPORTANT: call this from a background thread (Task.Run) — it does file I/O and
/// launches a process, neither of which should run on the Revit UI thread.
/// </summary>
public static class PatternBridge
{
    // ── Entry point ───────────────────────────────────────────────────────────

    public static Task NotifyAsync(RoutineDetectedEventArgs args)
    {
        try
        {
            App.DiagLog($"PatternBridge: preparing '{args.Label}' ({args.Count}×)");

            // Payload shape matches chatbot/chat_server.py PatternIn (/api/pattern).
            var payload = new JsonObject
            {
                ["label"]         = args.Label,
                ["count"]         = args.Count,
                ["motif"]         = BuildMotif(args.LatestEpisode),
                ["tool_sequence"] = BuildToolSequence(args.LatestEpisode),
                ["examples"]      = new JsonArray(),
            };

            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RevitPersonalization");
            Directory.CreateDirectory(dir);

            var patternPath = Path.Combine(dir, "pending_pattern.json");
            File.WriteAllText(patternPath, payload.ToJsonString());
            App.DiagLog($"PatternBridge: wrote {patternPath}");

            var (python, repoRoot) = ReadPythonConfig(dir);
            if (python is null || repoRoot is null)
            {
                App.DiagLog("PatternBridge: PYTHON_EXE / REPO_ROOT missing in .env — "
                          + "run 'python setup_revit_env.py' once. Pattern saved but assistant not launched.");
                return Task.CompletedTask;
            }

            var script = Path.Combine(repoRoot, "chatbot", "notify_from_file.py");
            if (!File.Exists(script))
            {
                App.DiagLog($"PatternBridge: notifier not found: {script}");
                return Task.CompletedTask;
            }

            var psi = new ProcessStartInfo
            {
                FileName         = python,
                UseShellExecute  = false,
                CreateNoWindow   = true,
                WorkingDirectory = repoRoot,
            };
            psi.ArgumentList.Add(script);
            psi.ArgumentList.Add(patternPath);

            Process.Start(psi);
            App.DiagLog("PatternBridge: launched chatbot notifier — BIM Assistant opening at http://localhost:5000");
        }
        catch (Exception ex)
        {
            App.DiagLog($"PatternBridge ERROR: {ex.GetType().Name}: {ex.Message}");
        }

        return Task.CompletedTask;
    }

    /// <summary>
    /// Reads PYTHON_EXE and REPO_ROOT from %LOCALAPPDATA%\RevitPersonalization\.env
    /// (written by setup_revit_env.py). Returns (null, null) if either is missing.
    /// </summary>
    private static (string? Python, string? RepoRoot) ReadPythonConfig(string dir)
    {
        try
        {
            var envPath = Path.Combine(dir, ".env");
            if (!File.Exists(envPath)) return (null, null);

            string? python = null, repo = null;
            foreach (var raw in File.ReadAllLines(envPath))
            {
                var line = raw.Trim();
                if (line.StartsWith("PYTHON_EXE="))
                    python = line["PYTHON_EXE=".Length..].Trim().Trim('"');
                else if (line.StartsWith("REPO_ROOT="))
                    repo = line["REPO_ROOT=".Length..].Trim().Trim('"');
            }
            return (python, repo);
        }
        catch { return (null, null); }
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
    // {{last_element_id}} is a placeholder resolved by revit_bridge.execute_shortcut
    // (Python) to the ElementId returned by the most recent place_element call.

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
    /// Matches the family_type format the chatbot tool_sequence / revit_bridge expects.
    /// </summary>
    private static string FamilyTypeLabel(ActionRecord a)
        => string.IsNullOrEmpty(a.TypeName)
            ? a.FamilyName
            : $"{a.FamilyName} : {a.TypeName}";
}
