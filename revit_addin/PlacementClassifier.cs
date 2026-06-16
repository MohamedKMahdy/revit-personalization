namespace RevitLogger;

/// <summary>
/// Decides whether a family's placement type is "point-based" — i.e. instantiated
/// at a single insertion point, which is the assumption behind the motif schema and
/// the Place-anchored episode segmentation in <see cref="RoutineDetector"/>.
///
/// This is the FEED gate for the detector. It is keyed on the *name* of
/// Autodesk.Revit.DB.FamilyPlacementType (via Enum.ToString()) deliberately, so the
/// classification logic is a pure string predicate with NO RevitAPI dependency and
/// can be unit-tested without loading RevitAPI.dll (which cannot load outside Revit).
///
/// Verified against C:\Program Files\Autodesk\Revit 2026\RevitAPI.dll
/// (FamilyPlacementType members + doc summaries):
///   POINT-BASED (feed):
///     OneLevelBased         single point on a level (furniture, casework, generic models)
///     OneLevelBasedHosted   single point + host (doors, windows, hosted fixtures)
///     TwoLevelsBased        single point spanning two levels (columns)
///     WorkPlaneBased        single point on a work plane / face
///     ViewBased             view-specific, point-placed (detail components)
///   NOT POINT-BASED (excluded):
///     Adaptive              multi-point adaptive component — no single insertion point
///     CurveBased            line-based on a work plane
///     CurveBasedDetail      line-based detail component
///     CurveDrivenStructural beam / brace / slanted column (line-based)
///     Invalid               sentinel
/// </summary>
internal static class PlacementClassifier
{
    // FamilyPlacementType names treated as point-based. Ordinal, case-sensitive —
    // these are exact enum member names, which are stable across Revit versions.
    private static readonly HashSet<string> PointBasedPlacementNames =
        new(StringComparer.Ordinal)
        {
            "OneLevelBased",
            "OneLevelBasedHosted",
            "TwoLevelsBased",
            "WorkPlaneBased",
            "ViewBased",
        };

    /// <summary>
    /// True iff <paramref name="placementTypeName"/> is the name of a point-based
    /// FamilyPlacementType member. Null/empty/unknown names return false.
    /// </summary>
    public static bool IsPointBased(string? placementTypeName)
        => placementTypeName is not null && PointBasedPlacementNames.Contains(placementTypeName);
}
