using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using Autodesk.Revit.UI;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;

namespace RevitLogger;

/// <summary>
/// Executes saved shortcut configs inside Revit via a file-based inter-process
/// communication (IPC) protocol.
///
/// Two-thread design:
///
///   FileSystemWatcher (background thread)
///     Monitors: %LOCALAPPDATA%\RevitPersonalization\ipc\pending_execution.json
///     Written by: Python revit_bridge.execute_shortcut()
///     On detection: reads the request → stores it → raises ExternalEvent
///
///   IExternalEventHandler.Execute() (Revit UI thread)
///     Reads the pending request
///     Loads shortcuts/{shortcut_id}.json (Python ShortcutConfig format)
///     Opens a Revit Transaction
///     Executes each step: Place FamilyInstance → SetParam × n → Tag
///     Writes: ipc/execution_result_{shortcut_id}.json (Python polls for this)
///
/// The ExternalEvent bridge is the standard Revit API pattern for driving API
/// calls from a background-thread notification without blocking the UI thread.
///
/// Shortcut config format: Python shared/schemas.py ShortcutConfig
///   { shortcut_id, name, motif: { steps: [ { action_type, family_name, param_name,
///     param_value, param_value_type, tag_family_name, level_name }, ... ] } }
/// </summary>
public sealed class ShortcutRunner : IExternalEventHandler, IDisposable
{
    // ── Directory paths ───────────────────────────────────────────────────────

    private static readonly string IpcDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "RevitPersonalization", "ipc");

    private static readonly string ShortcutsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "RevitPersonalization", "shortcuts");

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy        = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition      = JsonIgnoreCondition.WhenWritingNull,
    };

    // ── State ─────────────────────────────────────────────────────────────────

    private ExternalEvent?     _externalEvent;
    private FileSystemWatcher? _watcher;

    // Written by FSW thread (background), read by Execute() (UI thread).
    // Interlocked.Exchange is used for safe cross-thread handoff.
    private volatile ExecutionRequest? _pendingRequest;

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /// <summary>
    /// Registers the Revit ExternalEvent and starts the FileSystemWatcher.
    /// Call from App.OnStartup (valid Revit API context required for ExternalEvent.Create).
    /// </summary>
    public void Initialize()
    {
        _externalEvent = ExternalEvent.Create(this);

        Directory.CreateDirectory(IpcDir);
        Directory.CreateDirectory(ShortcutsDir);

        _watcher = new FileSystemWatcher(IpcDir, "pending_execution.json")
        {
            NotifyFilter        = NotifyFilters.FileName | NotifyFilters.LastWrite,
            EnableRaisingEvents = true,
        };
        _watcher.Created += OnIpcFile;
        _watcher.Changed += OnIpcFile;

        App.DiagLog($"ShortcutRunner initialized. IPC dir: {IpcDir}");
    }

    public void Dispose()
    {
        _watcher?.Dispose();
        _externalEvent?.Dispose();
    }

    // ── FileSystemWatcher callback ─────────────────────────────────────────────
    // Runs on a background thread. Must not call Revit API directly.

    private void OnIpcFile(object sender, FileSystemEventArgs e)
    {
        try
        {
            // Give Python time to finish flushing the JSON before we read it
            Thread.Sleep(200);

            var json = File.ReadAllText(e.FullPath, System.Text.Encoding.UTF8);
            var req  = JsonSerializer.Deserialize<ExecutionRequest>(json, JsonOpts);
            if (req is null) return;

            // Thread-safe handoff: overwrite any previous pending request
            Interlocked.Exchange(ref _pendingRequest, req);
            _externalEvent?.Raise();

            App.DiagLog($"ShortcutRunner: ExternalEvent raised — shortcut_id={req.ShortcutId}");
        }
        catch (Exception ex)
        {
            App.DiagLog($"ShortcutRunner.OnIpcFile error: {ex.Message}");
        }
    }

    // ── IExternalEventHandler ─────────────────────────────────────────────────
    // Execute() is called by Revit on the UI thread.

    public string GetName() => "RevitPersonalization.ShortcutRunner";

    public void Execute(UIApplication app)
    {
        // Claim the pending request atomically
        var request = Interlocked.Exchange(ref _pendingRequest, null);
        if (request is null) return;

        var pendingPath = Path.Combine(IpcDir, "pending_execution.json");
        var resultPath  = Path.Combine(IpcDir, $"execution_result_{request.ShortcutId}.json");

        // Remove request file so Python doesn't re-trigger
        try { if (File.Exists(pendingPath)) File.Delete(pendingPath); } catch { }

        var uidoc = app.ActiveUIDocument;
        var doc   = uidoc?.Document;
        if (doc is null || doc.IsFamilyDocument)
        {
            WriteResult(resultPath, false, "No active project document.", request.ShortcutId);
            return;
        }

        // Load shortcut config
        var configPath = Path.Combine(ShortcutsDir, $"{request.ShortcutId}.json");
        if (!File.Exists(configPath))
        {
            WriteResult(resultPath, false,
                $"Shortcut not found: {request.ShortcutId}", request.ShortcutId);
            return;
        }

        try
        {
            var configJson = File.ReadAllText(configPath, System.Text.Encoding.UTF8);
            var config     = JsonSerializer.Deserialize<ShortcutExecConfig>(configJson, JsonOpts)
                             ?? throw new InvalidOperationException("Null config after deserialize.");

            RunShortcut(uidoc!, doc, config, request.Params, resultPath);
        }
        catch (Exception ex)
        {
            App.DiagLog($"ShortcutRunner.Execute exception: {ex.Message}");
            WriteResult(resultPath, false, ex.Message, request.ShortcutId);
        }
    }

    // ── Transaction execution ─────────────────────────────────────────────────

    private static void RunShortcut(UIDocument uidoc, Document doc,
        ShortcutExecConfig config, Dictionary<string, JsonElement>? rp, string resultPath)
    {
        App.DiagLog($"RunShortcut: '{config.Name}' steps={config.Motif.Steps.Count}");
        using var tx = new Transaction(doc, $"Run Shortcut: {config.Name}");
        tx.Start();

        try
        {
            FamilyInstance? placed = null;

            foreach (var step in config.Motif.Steps)
            {
                App.DiagLog($"  step: {step.ActionType} family={step.FamilyName} param={step.ParamName}");
                switch (step.ActionType)
                {
                    case "Place":
                        placed = DoPlace(doc, step, rp);
                        if (placed is null)
                        {
                            tx.RollBack();
                            WriteResult(resultPath, false,
                                $"Could not place '{step.FamilyName}'. Family not loaded?",
                                config.ShortcutId);
                            return;
                        }
                        break;

                    case "SetParam" when placed is not null:
                        DoSetParam(placed, step, rp);
                        break;

                    case "Tag" when placed is not null:
                        DoTag(doc, uidoc.ActiveView, placed);
                        break;
                }
            }

            tx.Commit();
            var eid = placed is not null ? (int)placed.Id.Value : 0;
            App.DiagLog($"RunShortcut committed OK. element_id={eid}");
            WriteResult(resultPath, true, "OK", config.ShortcutId, eid);
        }
        catch (Exception ex)
        {
            if (tx.HasStarted() && !tx.HasEnded()) tx.RollBack();
            App.DiagLog($"RunShortcut transaction failed: {ex.Message}");
            WriteResult(resultPath, false, ex.Message, config.ShortcutId);
        }
    }

    // ── Step executors ────────────────────────────────────────────────────────

    private static FamilyInstance? DoPlace(Document doc, StepConfig step,
        Dictionary<string, JsonElement>? rp)
    {
        // Find a matching FamilySymbol
        FamilySymbol? symbol = null;
        foreach (var fs in new FilteredElementCollector(doc)
                               .OfClass(typeof(FamilySymbol))
                               .OfType<FamilySymbol>())
        {
            if (!string.Equals(fs.Family?.Name, step.FamilyName, StringComparison.OrdinalIgnoreCase))
                continue;
            if (!string.IsNullOrEmpty(step.TypeName) &&
                !string.Equals(fs.Name, step.TypeName, StringComparison.OrdinalIgnoreCase))
                continue;
            symbol = fs;
            break;
        }

        if (symbol is null)
        {
            App.DiagLog($"DoPlace: FamilySymbol '{step.FamilyName}/{step.TypeName}' not found in document.");
            return null;
        }

        if (!symbol.IsActive) { symbol.Activate(); doc.Regenerate(); }

        // Placement coordinates — use runtime params if supplied, else origin
        var x   = GetDouble(rp, "location_x", 0.0);
        var y   = GetDouble(rp, "location_y", 0.0);
        var z   = GetDouble(rp, "location_z", 0.0);
        var loc = new XYZ(x, y, z);

        // Find the target Level
        Level? level = null;
        var levelName = GetString(rp, "level_name", step.LevelName ?? "");
        if (!string.IsNullOrEmpty(levelName))
            level = new FilteredElementCollector(doc)
                        .OfClass(typeof(Level)).OfType<Level>()
                        .FirstOrDefault(l => string.Equals(l.Name, levelName,
                                             StringComparison.OrdinalIgnoreCase));

        // Fall back to the lowest level in the project
        level ??= new FilteredElementCollector(doc)
                      .OfClass(typeof(Level)).OfType<Level>()
                      .OrderBy(l => l.Elevation)
                      .FirstOrDefault();

        if (level is null)
        {
            App.DiagLog("DoPlace: no Level found in document.");
            return null;
        }

        return doc.Create.NewFamilyInstance(loc, symbol, level, StructuralType.NonStructural);
    }

    private static void DoSetParam(FamilyInstance fi, StepConfig step,
        Dictionary<string, JsonElement>? rp)
    {
        if (string.IsNullOrEmpty(step.ParamName)) return;

        var param = fi.LookupParameter(step.ParamName);
        if (param is null || param.IsReadOnly)
        {
            App.DiagLog($"DoSetParam: param '{step.ParamName}' not found or read-only.");
            return;
        }

        // Priority: runtime override > config constant value
        string? valueStr = null;
        if (rp?.TryGetValue(step.ParamName, out var rtEl) == true)
            valueStr = rtEl.ValueKind == JsonValueKind.String
                       ? rtEl.GetString()
                       : rtEl.ToString();
        else if (step.ParamValue is JsonElement pv && pv.ValueKind != JsonValueKind.Null)
            valueStr = pv.ValueKind == JsonValueKind.String
                       ? pv.GetString()
                       : pv.ToString();

        if (valueStr is null) return;

        switch (param.StorageType)
        {
            case StorageType.String:
                param.Set(valueStr);
                break;
            case StorageType.Double:
                if (double.TryParse(valueStr, System.Globalization.NumberStyles.Any,
                                    System.Globalization.CultureInfo.InvariantCulture, out var d))
                    param.Set(d);
                break;
            case StorageType.Integer:
                if (int.TryParse(valueStr, out var i))
                    param.Set(i);
                break;
        }
        App.DiagLog($"DoSetParam: {step.ParamName} = '{valueStr}'");
    }

    private static void DoTag(Document doc, View? view, FamilyInstance fi)
    {
        if (view is null) return;
        var pt     = (fi.Location as LocationPoint)?.Point ?? XYZ.Zero;
        var tagPt  = new XYZ(pt.X + 2.0, pt.Y + 2.0, pt.Z);
        try
        {
            IndependentTag.Create(doc, view.Id, new Reference(fi),
                false, TagMode.TM_ADDBY_CATEGORY, TagOrientation.Horizontal, tagPt);
            App.DiagLog($"DoTag: tagged element {fi.Id.Value}");
        }
        catch (Exception ex)
        {
            // Tag creation is best-effort — view type may not support tagging
            App.DiagLog($"DoTag (non-fatal): {ex.Message}");
        }
    }

    // ── Result writer ─────────────────────────────────────────────────────────

    private static void WriteResult(string path, bool success, string message,
        string shortcutId, int elementId = 0)
    {
        var obj = new
        {
            shortcut_id = shortcutId,
            success,
            message,
            element_id  = elementId,
            executed_at = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
        };
        var json = JsonSerializer.Serialize(obj, new JsonSerializerOptions { WriteIndented = false });
        File.WriteAllText(path, json, System.Text.Encoding.UTF8);
        App.DiagLog($"ShortcutRunner: result → {Path.GetFileName(path)} success={success}");
    }

    // ── JSON helpers ──────────────────────────────────────────────────────────

    private static double GetDouble(Dictionary<string, JsonElement>? p, string key, double def)
    {
        if (p?.TryGetValue(key, out var el) == true && el.ValueKind == JsonValueKind.Number
            && el.TryGetDouble(out var d))
            return d;
        return def;
    }

    private static string GetString(Dictionary<string, JsonElement>? p, string key, string def)
    {
        if (p?.TryGetValue(key, out var el) == true && el.ValueKind == JsonValueKind.String)
            return el.GetString() ?? def;
        return def;
    }
}

// ── IPC data model ────────────────────────────────────────────────────────────

internal class ExecutionRequest
{
    [JsonPropertyName("shortcut_id")] public string ShortcutId { get; set; } = "";
    [JsonPropertyName("params")]      public Dictionary<string, JsonElement>? Params { get; set; }
    [JsonPropertyName("requested_at")] public double RequestedAt { get; set; }
}

// ── Shortcut config (C# mirror of Python ShortcutConfig in shared/schemas.py) ─

internal class ShortcutExecConfig
{
    [JsonPropertyName("shortcut_id")] public string          ShortcutId { get; set; } = "";
    [JsonPropertyName("name")]        public string          Name       { get; set; } = "";
    [JsonPropertyName("motif")]       public MotifExecConfig Motif      { get; set; } = new();
}

internal class MotifExecConfig
{
    [JsonPropertyName("name")]  public string          Name  { get; set; } = "";
    [JsonPropertyName("steps")] public List<StepConfig> Steps { get; set; } = new();
}

internal class StepConfig
{
    [JsonPropertyName("action_type")]      public string       ActionType     { get; set; } = "";
    [JsonPropertyName("family_name")]      public string       FamilyName     { get; set; } = "";
    [JsonPropertyName("type_name")]        public string       TypeName       { get; set; } = "";
    [JsonPropertyName("param_name")]       public string       ParamName      { get; set; } = "";
    [JsonPropertyName("param_value")]      public JsonElement? ParamValue     { get; set; }
    [JsonPropertyName("param_value_type")] public string       ParamValueType { get; set; } = "";
    [JsonPropertyName("tag_family_name")]  public string       TagFamilyName  { get; set; } = "";
    [JsonPropertyName("level_name")]       public string?      LevelName      { get; set; }
}
