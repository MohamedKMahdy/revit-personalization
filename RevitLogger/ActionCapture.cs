using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Events;

namespace RevitLogger;

/// <summary>
/// Translates Revit DocumentChangedEventArgs into enriched ActionRecord objects
/// and forwards them to the LogWriter.
///
/// App owns the application-level DocumentChanged subscription and calls
/// ProcessEvent only for the document that belongs to this session.
///
/// Revit 2027 API changes applied:
///   • ElementId.Value (long) replaces the removed ElementId.IntegerValue (int)
///   • IndependentTag.GetTaggedLocalElement() removed →
///       GetTaggedReferences() used instead (returns IList&lt;Reference&gt;)
///   • IndependentTag.Symbol removed →
///       doc.GetElement(tag.GetTypeId()) as FamilySymbol used instead
///
/// Schema design — Jang &amp; Lee (2023) arXiv:2305.18032 + Jang et al. (2023) AEI 57:
///   • transaction_id groups all records from one Revit transaction
///   • transaction_name surfaces the undo-label (e.g. "Place Door")
///   • Tags only emitted on AddedElementIds — modify events (leader drags) are noise
/// </summary>
public class ActionCapture : IDisposable
{
    private readonly LogWriter        _writer;
    private readonly RoutineDetector? _detector;   // null when real-time detection is off
    private readonly ElementSnapshot  _snapshot = new();
    private bool _disposed;

    private static readonly HashSet<string> AuthoringCategories =
        new(StringComparer.OrdinalIgnoreCase)
        {
            "Doors", "Windows",
            "Structural Columns", "Structural Framing", "Structural Foundations",
            "Furniture", "Furniture Systems", "Casework",
            "Mechanical Equipment", "Plumbing Fixtures", "Electrical Equipment",
            "Lighting Fixtures", "Specialty Equipment", "Generic Models",
            "Walls", "Floors", "Roofs", "Ceilings",
            "Columns", "Stairs", "Railings",
        };

    public ActionCapture(LogWriter writer, RoutineDetector? detector = null)
    {
        _writer   = writer;
        _detector = detector;
    }

    public void Dispose() { _disposed = true; }

    // ── Entry point ───────────────────────────────────────────────────────

    public void ProcessEvent(DocumentChangedEventArgs e)
    {
        if (_disposed) return;
        var doc = e.GetDocument();
        if (doc.IsFamilyDocument) return;

        var txId   = Guid.NewGuid().ToString("N")[..12];
        var txName = string.Join("; ", e.GetTransactionNames());
        var now    = DateTime.UtcNow;

        var ctx = new RecordContext(
            TransactionId:   txId,
            TransactionName: txName,
            TimestampUtc:    now.ToString("o"),
            TimestampUnix:   new DateTimeOffset(now).ToUnixTimeMilliseconds() / 1000.0,
            ViewId:          (int)(doc.ActiveView?.Id?.Value ?? 0),
            ViewName:        doc.ActiveView?.Name ?? "",
            ViewType:        doc.ActiveView?.ViewType.ToString() ?? ""
        );

        foreach (var id in e.GetAddedElementIds())
            HandleAdded(doc, id, ctx);

        foreach (var id in e.GetModifiedElementIds())
            HandleModified(doc, id, ctx);

        foreach (var id in e.GetDeletedElementIds())
            _snapshot.Remove(id.Value);
    }

    // ── Per-element handlers ──────────────────────────────────────────────

    private void HandleAdded(Document doc, ElementId id, RecordContext ctx)
    {
        var el = doc.GetElement(id);
        if (el is null) return;

        // Tags only on add — modify events are leader repositioning, not authoring
        if (el is IndependentTag tag)
        {
            EmitTag(doc, tag, ctx);
            return;
        }

        if (el is FamilyInstance fi && IsAuthoring(fi))
        {
            EmitPlace(fi, ctx);
            _snapshot.Snapshot(fi);
        }
    }

    private void HandleModified(Document doc, ElementId id, RecordContext ctx)
    {
        var el = doc.GetElement(id);
        if (el is null) return;

        // Do NOT re-emit tags on modify (noise suppression)
        if (el is FamilyInstance fi && IsAuthoring(fi))
        {
            foreach (var change in _snapshot.GetChanges(fi))
                EmitSetParam(fi, change, ctx);
        }
    }

    // ── Record emitters ───────────────────────────────────────────────────

    private void EmitPlace(FamilyInstance fi, RecordContext ctx)
    {
        var r = BaseRecord("Place", OperationClass.Model, fi, ctx);
        r.HostCategory = fi.Host?.Category?.Name;
        _writer.Enqueue(r);
        _detector?.Feed(r);   // real-time episode tracking
    }

    private void EmitSetParam(FamilyInstance fi, ParamChange change, RecordContext ctx)
    {
        var r = BaseRecord("SetParam", OperationClass.Parameter, fi, ctx);
        r.ParamName        = change.Name;
        r.ParamStorageType = change.StorageType;
        r.ParamValueBefore = change.Before;
        r.ParamValueAfter  = change.After;
        _writer.Enqueue(r);
        _detector?.Feed(r);   // real-time episode tracking
    }

    private void EmitTag(Document doc, IndependentTag tag, RecordContext ctx)
    {
        // Revit 2027: GetTaggedLocalElement() removed — use GetTaggedReferences()
        Element? tagged = null;
        try
        {
            var refs = tag.GetTaggedReferences();
            if (refs?.Count > 0)
                tagged = doc.GetElement(refs[0]);
        }
        catch { /* some tags may not have a reference */ }

        // Revit 2027: IndependentTag.Symbol removed — retrieve via GetTypeId()
        var tagType  = doc.GetElement(tag.GetTypeId()) as FamilySymbol;
        var famName  = tagType?.Family?.Name ?? tag.Category?.Name ?? "";
        var typeName = tagType?.Name ?? "";

        var tagRecord = new ActionRecord
        {
            SessionId       = _writer.SessionId,
            TransactionId   = ctx.TransactionId,
            TransactionName = ctx.TransactionName,
            TimestampUtc    = ctx.TimestampUtc,
            TimestampUnix   = ctx.TimestampUnix,
            ActionType      = "Tag",
            OperationClass  = OperationClass.Annotation,
            ElementId       = (int)tag.Id.Value,
            ElementCategory = tag.Category?.Name ?? "",
            FamilyName      = famName,
            TypeName        = typeName,
            LevelName       = LevelOf(tag),
            ViewId          = ctx.ViewId,
            ViewName        = ctx.ViewName,
            ViewType        = ctx.ViewType,
            TagFamilyName   = famName,
            TaggedElementId = tagged is not null ? (int)tagged.Id.Value : null,
        };
        _writer.Enqueue(tagRecord);
        _detector?.Feed(tagRecord);   // may complete an episode → fire RoutineDetected
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    private ActionRecord BaseRecord(string actionType, OperationClass opClass,
                                    FamilyInstance fi, RecordContext ctx) => new()
    {
        SessionId       = _writer.SessionId,
        TransactionId   = ctx.TransactionId,
        TransactionName = ctx.TransactionName,
        TimestampUtc    = ctx.TimestampUtc,
        TimestampUnix   = ctx.TimestampUnix,
        ActionType      = actionType,
        OperationClass  = opClass,
        ElementId       = (int)fi.Id.Value,
        ElementCategory = fi.Category?.Name ?? "",
        FamilyName      = fi.Symbol?.Family?.Name ?? "",
        TypeName        = fi.Symbol?.Name ?? "",
        LevelName       = LevelOf(fi),
        PhaseName       = fi.get_Parameter(BuiltInParameter.PHASE_CREATED)
                            ?.AsValueString() ?? "",
        ViewId          = ctx.ViewId,
        ViewName        = ctx.ViewName,
        ViewType        = ctx.ViewType,
    };

    private static bool IsAuthoring(FamilyInstance fi)
        => AuthoringCategories.Contains(fi.Category?.Name ?? "");

    private static string LevelOf(Element el)
    {
        var lvlId = el.LevelId;
        if (lvlId is not null && lvlId != ElementId.InvalidElementId)
            return (el.Document.GetElement(lvlId) as Level)?.Name ?? "";
        return "";
    }
}

internal record RecordContext(
    string TransactionId,
    string TransactionName,
    string TimestampUtc,
    double TimestampUnix,
    int    ViewId,
    string ViewName,
    string ViewType
);
