using Autodesk.Revit.DB;

namespace RevitLogger;

/// <summary>
/// In-memory parameter-value cache for tracked elements.
///
/// Enables before/after diffs required by the enhanced logging schema
/// (Jang &amp; Lee 2023 arXiv:2305.18032): when DocumentChanged fires we compare
/// current values against the cached snapshot and emit only changed params.
///
/// Revit 2027 API notes applied here:
///   • ElementId.Value (long) replaces the removed ElementId.IntegerValue (int)
///   • LabelUtils.GetLabelFor(ForgeTypeId) is not available — group labels are
///     omitted; filtering uses the parameter name blacklist instead.
/// </summary>
public class ElementSnapshot
{
    // element_id (as long) → (paramName → value)
    private readonly Dictionary<long, Dictionary<string, object?>> _cache = new();

    /// <summary>
    /// Parameters excluded from tracking — auto-computed, spatial, or Revit-internal
    /// values that are not user-authored (Jang &amp; Lee 2023 reproducibility criteria).
    /// </summary>
    private static readonly HashSet<string> IgnoredParamNames =
        new(StringComparer.OrdinalIgnoreCase)
        {
            "Area", "Volume", "Perimeter",
            "Phase Created", "Phase Demolished",
            "Work Plane", "Host", "Workset", "Design Option",
            "Image", "Moves With Nearby Elements",
            "Room: Name", "Room: Number", "Space: Name", "Space: Number",
            "Family", "Family and Type",
        };

    // ── Public API ────────────────────────────────────────────────────────

    /// <summary>
    /// Snapshot current parameter values immediately after a Place event,
    /// giving a clean baseline for subsequent SetParam diff detection.
    /// </summary>
    public void Snapshot(Element el)
        => _cache[el.Id.Value] = ExtractValues(el);

    /// <summary>
    /// Return the list of parameters that changed since the last snapshot.
    /// Updates the cache eagerly (not lazily) so it is always consistent.
    /// </summary>
    public IReadOnlyList<ParamChange> GetChanges(Element el)
    {
        var key     = el.Id.Value;
        var current = ExtractValues(el);
        var changes = new List<ParamChange>();

        if (_cache.TryGetValue(key, out var cached))
        {
            foreach (var (name, newVal) in current)
            {
                var oldVal = cached.GetValueOrDefault(name);
                if (!ValuesEqual(oldVal, newVal))
                {
                    var storageType = GetStorageType(el, name);
                    changes.Add(new ParamChange(name, storageType, oldVal, newVal));
                }
            }
        }

        _cache[key] = current;   // always update before returning
        return changes;
    }

    /// <summary>Remove an element from the cache on deletion.</summary>
    public void Remove(long elementId) => _cache.Remove(elementId);

    // ── Helpers ───────────────────────────────────────────────────────────

    private static Dictionary<string, object?> ExtractValues(Element el)
    {
        var result = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
        foreach (Parameter p in el.Parameters)
        {
            if (!ShouldTrack(p)) continue;
            result[p.Definition.Name] = ReadValue(p);
        }
        return result;
    }

    private static bool ShouldTrack(Parameter p)
    {
        if (p.IsReadOnly) return false;
        if (p.StorageType is StorageType.None or StorageType.ElementId) return false;
        if (IgnoredParamNames.Contains(p.Definition.Name)) return false;
        return true;
    }

    private static object? ReadValue(Parameter p) => p.StorageType switch
    {
        StorageType.String  => p.AsString(),
        StorageType.Integer => (object)p.AsInteger(),
        // Internal unit is decimal feet → convert to mm, rounded to nearest mm
        StorageType.Double  => (object)Math.Round(p.AsDouble() * 304.8, 0),
        _                   => null,
    };

    private static string GetStorageType(Element el, string name)
    {
        foreach (Parameter p in el.Parameters)
        {
            if (string.Equals(p.Definition.Name, name, StringComparison.OrdinalIgnoreCase))
                return p.StorageType.ToString();
        }
        return "";
    }

    private static bool ValuesEqual(object? a, object? b)
    {
        if (a is null && b is null) return true;
        if (a is null || b is null) return false;
        return a.ToString() == b.ToString();
    }
}

/// <summary>A single detected parameter change: name, storage type, before and after values.</summary>
public record ParamChange(
    string  Name,
    string  StorageType,
    object? Before,
    object? After
);
