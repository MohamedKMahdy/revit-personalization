using System.Text.Json.Serialization;

namespace RevitLogger;

/// <summary>
/// Operation class taxonomy — Jang et al. (2023) AEI 57, 102079 lexicon.
/// </summary>
[JsonConverter(typeof(JsonStringEnumConverter))]
public enum OperationClass { Model, Parameter, Annotation, View }

/// <summary>
/// Enriched BIM action record.
///
/// Schema follows Jang &amp; Lee (2023) arXiv:2305.18032 enhanced BIM logging:
///   • transaction_id / transaction_name  — atomic grouping
///   • param_value_before / param_value_after — reproducible diffs
///   • level_name, view_type, phase_name  — rich spatial/temporal context
///
/// Field names use snake_case, matching the Python shared/schemas.py model.
/// Serialised with System.Text.Json (built into .NET 8, no NuGet dependency).
/// </summary>
public class ActionRecord
{
    // ── Schema / identity ─────────────────────────────────────────────────
    [JsonPropertyName("schema_version")]
    public string SchemaVersion { get; } = "2.0";

    [JsonPropertyName("event_id")]
    public string EventId { get; set; } = Guid.NewGuid().ToString("N")[..12];

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    [JsonPropertyName("transaction_id")]
    public string TransactionId { get; set; } = "";

    /// <summary>
    /// Revit undo-stack label — equivalent to Jang &amp; Lee (2023) "transaction_name".
    /// e.g. "Place Door", "Modify Parameters", "Tag Element".
    /// </summary>
    [JsonPropertyName("transaction_name")]
    public string TransactionName { get; set; } = "";

    // ── Timing ────────────────────────────────────────────────────────────
    [JsonPropertyName("timestamp_utc")]
    public string TimestampUtc { get; set; } = "";

    [JsonPropertyName("timestamp_unix")]
    public double TimestampUnix { get; set; }

    // ── Action taxonomy (Jang et al. 2023 lexicon) ───────────────────────
    [JsonPropertyName("action_type")]
    public string ActionType { get; set; } = "";      // Place | SetParam | Tag | Delete

    [JsonPropertyName("operation_class")]
    public OperationClass OperationClass { get; set; }  // Model | Parameter | Annotation

    // ── Element context ───────────────────────────────────────────────────
    [JsonPropertyName("element_id")]
    public int ElementId { get; set; }

    [JsonPropertyName("element_category")]
    public string ElementCategory { get; set; } = "";

    [JsonPropertyName("family_name")]
    public string FamilyName { get; set; } = "";

    [JsonPropertyName("type_name")]
    public string TypeName { get; set; } = "";

    [JsonPropertyName("level_name")]
    public string LevelName { get; set; } = "";

    [JsonPropertyName("phase_name")]
    public string PhaseName { get; set; } = "";

    [JsonPropertyName("host_category")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? HostCategory { get; set; }

    // ── View context ──────────────────────────────────────────────────────
    [JsonPropertyName("view_id")]
    public int ViewId { get; set; }

    [JsonPropertyName("view_name")]
    public string ViewName { get; set; } = "";

    [JsonPropertyName("view_type")]
    public string ViewType { get; set; } = "";

    // ── SetParam fields ───────────────────────────────────────────────────
    [JsonPropertyName("param_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ParamName { get; set; }

    [JsonPropertyName("param_group")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ParamGroup { get; set; }

    [JsonPropertyName("param_storage_type")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ParamStorageType { get; set; }

    /// <summary>Value before this transaction — null for newly placed elements.</summary>
    [JsonPropertyName("param_value_before")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public object? ParamValueBefore { get; set; }

    [JsonPropertyName("param_value_after")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public object? ParamValueAfter { get; set; }

    // ── Geometry fields (supervisor's GeometryExtractor §3.3) ────────────
    /// <summary>
    /// Revit internal units (decimal feet). Populated for Place events only.
    /// LocationPoint for hosted elements (Doors, Windows, Furniture);
    /// curve midpoint for linear elements (Walls, Beams).
    /// </summary>
    [JsonPropertyName("location_x")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public double? LocationX { get; set; }

    [JsonPropertyName("location_y")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public double? LocationY { get; set; }

    [JsonPropertyName("location_z")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public double? LocationZ { get; set; }

    // ── Host / orientation (Doors, Windows — supervisor's CategoryExtractor) ─
    /// <summary>ElementId of the hosting element (e.g. the Wall a Door lives in).</summary>
    [JsonPropertyName("host_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? HostId { get; set; }

    /// <summary>True when the element's facing direction is flipped relative to its host.</summary>
    [JsonPropertyName("flip_facing")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? FlipFacing { get; set; }

    /// <summary>True when the element's hand (swing) direction is flipped relative to its host.</summary>
    [JsonPropertyName("flip_hand")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? FlipHand { get; set; }

    // ── Tag / Annotation fields ───────────────────────────────────────────
    [JsonPropertyName("tag_family_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? TagFamilyName { get; set; }

    [JsonPropertyName("tagged_element_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? TaggedElementId { get; set; }
}
