using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Architecture;
using Autodesk.Revit.DB.Events;

namespace RevitLogger;

/// <summary>
/// Translates Revit DocumentChangedEventArgs into enriched ActionRecord objects
/// and forwards them to the LogWriter (and optionally to the RoutineDetector).
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
/// Extensions from supervisor's generalBIMlog (suhyungJang/generalBIMlog):
///   • Sketch-based elements (Floor, Ceiling, Roof, Railing): Revit fires Added
///     while sketch is open; we defer the CREATED record until Modified fires
///     (i.e., the sketch has been submitted).  Tracked via _sketchPendingIds.
///   • Stair elements (Stairs, StairsRun, StairsLanding): Added fires while the
///     stair editor is active (IsInEditMode() == true); deferred via _stairPendingIds.
///   • DELETED events: _elemInfoCache stores (category, family, type) for every
///     logged element so that DELETED records can be reconstructed after removal.
///   • Non-FamilyInstance elements (Wall, Level, Grid): logged for general audit;
///     not fed into RoutineDetector (CEI episodes are FamilyInstance-only).
///   • Geometry (LocationX/Y/Z): extracted from LocationPoint or curve midpoint.
///   • Host info (HostId, FlipFacing, FlipHand): populated for Doors and Windows.
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

    // Cache: elementId → (category, family, type) for DELETED record reconstruction.
    // After deletion doc.GetElement() returns null, so we must cache before the event.
    private readonly Dictionary<long, (string Category, string Family, string Type)>
        _elemInfoCache = new();

    // Sketch-based elements (Floor, Ceiling, Roof, Railing):
    // Revit fires Added mid-sketch (element exists but boundary is not closed).
    // The element fires Modified only when the user finishes the sketch.
    // We defer logging the CREATED record until that first Modified event.
    private readonly HashSet<long> _sketchPendingIds = new();

    // Stair elements (Stairs, StairsRun, StairsLanding):
    // Revit fires Added while the stair editor is active (IsInEditMode() == true).
    // We defer until Modified fires after the user exits the stair editor.
    private readonly HashSet<long> _stairPendingIds = new();

    // FamilyInstance categories relevant to CEI (Custom Element Instantiation) detection.
    // These are the element types that participate in Place → SetParam → Tag episodes.
    // Non-FamilyInstance element types (Walls, Floors, etc.) are handled separately.
    private static readonly HashSet<string> AuthoringCategories =
        new(StringComparer.OrdinalIgnoreCase)
        {
            "Doors", "Windows",
            "Structural Columns", "Structural Framing", "Structural Foundations",
            "Furniture", "Furniture Systems", "Casework",
            "Mechanical Equipment", "Plumbing Fixtures", "Electrical Equipment",
            "Lighting Fixtures", "Specialty Equipment", "Generic Models",
            "Columns",
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
            HandleDeleted(id);
    }

    // ── Per-element handlers ──────────────────────────────────────────────

    private void HandleAdded(Document doc, ElementId id, RecordContext ctx)
    {
        var el = doc.GetElement(id);
        if (el is null || el.Category is null) return;

        // Tags — emit immediately on add; modify events are just repositioning noise
        if (el is IndependentTag tag)
        {
            EmitTag(doc, tag, ctx);
            return;
        }

        var cat = el.Category.BuiltInCategory;

        // ── Sketch-based elements: defer until sketch exits ────────────────
        // The element fires Modified only once — when the user finishes the sketch.
        // We hold it in _sketchPendingIds until that happens.
        if (cat is BuiltInCategory.OST_Floors
                or BuiltInCategory.OST_Ceilings
                or BuiltInCategory.OST_Roofs
                or BuiltInCategory.OST_StairsRailing)
        {
            _sketchPendingIds.Add(id.Value);
            return;
        }

        // ── Stair elements: defer until editor exits ──────────────────────
        if (cat is BuiltInCategory.OST_Stairs
                or BuiltInCategory.OST_StairsRuns
                or BuiltInCategory.OST_StairsLandings)
        {
            _stairPendingIds.Add(id.Value);
            return;
        }

        // ── FamilyInstance (Doors, Windows, Furniture, …) — CEI tracking ──
        if (el is FamilyInstance fi && IsAuthoring(fi))
        {
            EmitPlace(fi, ctx);
            _snapshot.Snapshot(fi);
            return;
        }

        // ── Walls ─────────────────────────────────────────────────────────
        if (el is Wall wall)
        {
            EmitGeneralPlace(doc, wall, ctx);
            _snapshot.Snapshot(wall);
            return;
        }

        // ── Levels ────────────────────────────────────────────────────────
        if (el is Level)
        {
            EmitGeneralPlace(doc, el, ctx);
            return;
        }

        // ── Grids ─────────────────────────────────────────────────────────
        if (el is Grid)
        {
            EmitGeneralPlace(doc, el, ctx);
            return;
        }
    }

    private void HandleModified(Document doc, ElementId id, RecordContext ctx)
    {
        var el = doc.GetElement(id);
        if (el is null || el.Category is null) return;

        // ── Finalize sketch-based elements ────────────────────────────────
        // The first Modified event fires when the user clicks "Finish Sketch".
        // The Floor/Ceiling/Roof/Railing element does NOT fire Modified during
        // sketch editing — only sketch line elements do — so any Modified here
        // means the element boundary is now fully committed.
        if (_sketchPendingIds.Contains(id.Value))
        {
            _sketchPendingIds.Remove(id.Value);
            EmitGeneralPlace(doc, el, ctx);
            _snapshot.Snapshot(el);
            return;
        }

        // ── Finalize stair elements ────────────────────────────────────────
        if (_stairPendingIds.Contains(id.Value))
        {
            if (IsStairComplete(el))
            {
                _stairPendingIds.Remove(id.Value);
                EmitGeneralPlace(doc, el, ctx);
                _snapshot.Snapshot(el);
            }
            // Still in edit mode — skip until next Modified
            return;
        }

        // ── FamilyInstance: detect SetParam changes ───────────────────────
        if (el is FamilyInstance fi && IsAuthoring(fi))
        {
            foreach (var change in _snapshot.GetChanges(fi))
                EmitSetParam(fi, change, ctx);
            return;
        }

        // ── Wall: detect parameter changes ────────────────────────────────
        if (el is Wall wall)
        {
            foreach (var change in _snapshot.GetChanges(wall))
                EmitSetParamGeneral(wall, change, ctx);
        }
    }

    private void HandleDeleted(ElementId id)
    {
        var elemId = id.Value;
        _snapshot.Remove(elemId);
        _sketchPendingIds.Remove(elemId);
        _stairPendingIds.Remove(elemId);

        if (_elemInfoCache.TryGetValue(elemId, out var info))
        {
            EmitDeleted(elemId, info.Category, info.Family, info.Type);
            _elemInfoCache.Remove(elemId);
        }
        // Elements never in our cache (never logged) are silently ignored
    }

    // ── Record emitters ───────────────────────────────────────────────────

    private void EmitPlace(FamilyInstance fi, RecordContext ctx)
    {
        var r = BaseRecord("Place", OperationClass.Model, fi, ctx);

        // Host info — Doors and Windows are hosted in Walls
        if (fi.Host is not null)
        {
            r.HostCategory = fi.Host.Category?.Name;
            r.HostId       = fi.Host.Id.Value;
        }

        // Flip orientation — relevant for Doors, Windows
        r.FlipFacing = fi.FacingFlipped;
        r.FlipHand   = fi.HandFlipped;

        // Geometry: LocationPoint preferred; fall back to curve midpoint for beams
        ExtractLocation(fi, r);

        // Cache element info so we can reconstruct a DELETED record later
        _elemInfoCache[fi.Id.Value] = (r.ElementCategory, r.FamilyName, r.TypeName);

        _writer.Enqueue(r);
        _detector?.Feed(r);   // real-time CEI episode tracking
    }

    private void EmitSetParam(FamilyInstance fi, ParamChange change, RecordContext ctx)
    {
        var r = BaseRecord("SetParam", OperationClass.Parameter, fi, ctx);
        r.ParamName        = change.Name;
        r.ParamStorageType = change.StorageType;
        r.ParamValueBefore = change.Before;
        r.ParamValueAfter  = change.After;
        _writer.Enqueue(r);
        _detector?.Feed(r);   // real-time CEI episode tracking
    }

    /// <summary>
    /// SetParam record for non-FamilyInstance elements (Walls, etc.).
    /// Does not feed RoutineDetector — these changes are logged only, not detected as CEI.
    /// </summary>
    private void EmitSetParamGeneral(Element el, ParamChange change, RecordContext ctx)
    {
        var r = new ActionRecord
        {
            SessionId        = _writer.SessionId,
            TransactionId    = ctx.TransactionId,
            TransactionName  = ctx.TransactionName,
            TimestampUtc     = ctx.TimestampUtc,
            TimestampUnix    = ctx.TimestampUnix,
            ActionType       = "SetParam",
            OperationClass   = OperationClass.Parameter,
            ElementId        = (int)el.Id.Value,
            ElementCategory  = el.Category?.Name ?? "",
            FamilyName       = el.get_Parameter(BuiltInParameter.ELEM_FAMILY_PARAM)?.AsValueString() ?? "",
            TypeName         = el.get_Parameter(BuiltInParameter.ELEM_TYPE_PARAM)?.AsValueString()   ?? "",
            LevelName        = LevelOf(el),
            ViewId           = ctx.ViewId,
            ViewName         = ctx.ViewName,
            ViewType         = ctx.ViewType,
            ParamName        = change.Name,
            ParamStorageType = change.StorageType,
            ParamValueBefore = change.Before,
            ParamValueAfter  = change.After,
        };
        _writer.Enqueue(r);
    }

    /// <summary>
    /// Place record for non-FamilyInstance elements (Wall, Floor, Roof, Ceiling, Level, Grid,
    /// Stairs, StairsRun, StairsLanding, Railing).
    /// Does not feed RoutineDetector — these are logged for audit, not CEI detection.
    /// </summary>
    private void EmitGeneralPlace(Document doc, Element el, RecordContext ctx)
    {
        var cat = el.Category?.Name ?? "";
        var fam = el.get_Parameter(BuiltInParameter.ELEM_FAMILY_PARAM)?.AsValueString() ?? "";
        var typ = el.get_Parameter(BuiltInParameter.ELEM_TYPE_PARAM)?.AsValueString()   ?? "";

        var r = new ActionRecord
        {
            SessionId       = _writer.SessionId,
            TransactionId   = ctx.TransactionId,
            TransactionName = ctx.TransactionName,
            TimestampUtc    = ctx.TimestampUtc,
            TimestampUnix   = ctx.TimestampUnix,
            ActionType      = "Place",
            OperationClass  = OperationClass.Model,
            ElementId       = (int)el.Id.Value,
            ElementCategory = cat,
            FamilyName      = fam,
            TypeName        = typ,
            LevelName       = LevelOf(el),
            ViewId          = ctx.ViewId,
            ViewName        = ctx.ViewName,
            ViewType        = ctx.ViewType,
        };

        ExtractLocation(el, r);

        _elemInfoCache[el.Id.Value] = (cat, fam, typ);

        _writer.Enqueue(r);
    }

    /// <summary>
    /// Emit a DELETED record using cached element metadata.
    /// Does NOT feed RoutineDetector — deletions interrupt CEI episodes, not extend them.
    /// </summary>
    private void EmitDeleted(long elementId, string category, string family, string type)
    {
        var now = DateTime.UtcNow;
        var r = new ActionRecord
        {
            SessionId       = _writer.SessionId,
            TransactionId   = Guid.NewGuid().ToString("N")[..12],
            TransactionName = "Delete",
            TimestampUtc    = now.ToString("o"),
            TimestampUnix   = new DateTimeOffset(now).ToUnixTimeMilliseconds() / 1000.0,
            ActionType      = "Delete",
            OperationClass  = OperationClass.Model,
            ElementId       = (int)elementId,
            ElementCategory = category,
            FamilyName      = family,
            TypeName        = type,
        };
        _writer.Enqueue(r);
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
        _detector?.Feed(tagRecord);   // may complete a CEI episode → fire RoutineDetected
    }

    // ── Sketch / edit-mode completion detection ───────────────────────────

    /// <summary>
    /// Returns true when a stair-related element has exited edit mode.
    /// For StairsRun/StairsLanding we look up the parent Stairs element.
    /// </summary>
    private static bool IsStairComplete(Element el)
    {
        try
        {
            if (el is Stairs stair)
                return !stair.IsInEditMode();

            if (el is StairsRun run)
            {
                // GetStairs() returns the parent Stairs object directly
                var parent = run.GetStairs();
                return !(parent?.IsInEditMode() ?? false);
            }

            if (el is StairsLanding landing)
            {
                var parent = landing.GetStairs();
                return !(parent?.IsInEditMode() ?? false);
            }
        }
        catch { /* API may throw when element state is undefined */ }

        return false;
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    /// <summary>
    /// Fills LocationX/Y/Z from the element's Location property.
    /// Uses LocationPoint for hosted/point-based elements.
    /// Uses curve midpoint for linear elements (walls, beams).
    /// Coordinates are Revit internal units (decimal feet).
    /// </summary>
    private static void ExtractLocation(Element el, ActionRecord r)
    {
        try
        {
            if (el.Location is LocationPoint lp)
            {
                r.LocationX = lp.Point.X;
                r.LocationY = lp.Point.Y;
                r.LocationZ = lp.Point.Z;
            }
            else if (el.Location is LocationCurve lc)
            {
                var mid = lc.Curve.Evaluate(0.5, true);
                r.LocationX = mid.X;
                r.LocationY = mid.Y;
                r.LocationZ = mid.Z;
            }
        }
        catch { /* geometry may be unavailable for elements in sketch/edit mode */ }
    }

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
