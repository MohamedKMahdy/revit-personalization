using RevitLogger;
using Xunit;

namespace RevitLogger.Tests;

/// <summary>
/// End-to-end (gate + detector) behaviour using synthetic ActionRecords:
///   • a point-based family instance of a category OUTSIDE the old AuthoringCategories
///     list (Planting) now feeds and forms a routine;
///   • a curve-based instance and a wall (sketch-based system family) do not feed,
///     so they cannot form a routine.
/// The feed decision is PlacementClassifier (what ActionCapture consults before calling
/// RoutineDetector.Feed); routine formation is RoutineDetector itself.
/// </summary>
public class DetectorRoutineTests
{
    private static ActionRecord Place(int id, string category, string family) => new()
    {
        ActionType = "Place", ElementId = id, ElementCategory = category, FamilyName = family,
    };

    private static ActionRecord SetParam(int id, string param) => new()
    {
        ActionType = "SetParam", ElementId = id, ParamName = param,
    };

    private static ActionRecord Tag(int tagId, int taggedId, string tagFamily) => new()
    {
        ActionType = "Tag", ElementId = tagId, TaggedElementId = taggedId, TagFamilyName = tagFamily,
    };

    [Fact]
    public void PointBased_category_outside_legacy_list_feeds_and_forms_routine()
    {
        // "Planting" is NOT in the legacy AuthoringCategories list, but planting families
        // are point-based (OneLevelBased) — so the gate now lets them feed.
        Assert.True(PlacementClassifier.IsPointBased("OneLevelBased"));

        var detector = new RoutineDetector();
        RoutineDetectedEventArgs? fired = null;
        detector.RoutineDetected += (_, e) => fired = e;

        // Two complete Place → SetParam(Mark) → Tag episodes for the same Planting family.
        foreach (var (elemId, tagId) in new[] { (101, 201), (111, 211) })
        {
            detector.Feed(Place(elemId, "Planting", "Tree-A"));
            detector.Feed(SetParam(elemId, "Mark"));
            detector.Feed(Tag(tagId, elemId, "Planting Tag"));
        }

        Assert.NotNull(fired);
        Assert.Equal(2, fired!.Count);
        Assert.Contains("Planting", fired.Signature);
        Assert.Contains("SetParam(Mark)", fired.Signature);
    }

    [Fact]
    public void CurveBased_instance_and_wall_do_not_feed()
    {
        // Curve-based family instances (e.g. structural framing beams, line-based MEP)
        // are excluded by the placement gate, so ActionCapture would never feed them.
        Assert.False(PlacementClassifier.IsPointBased("CurveDrivenStructural"));
        Assert.False(PlacementClassifier.IsPointBased("CurveBased"));

        // A wall is a sketch-based SYSTEM family — it is not a FamilyInstance and has no
        // FamilyPlacementType, so it never even reaches the classifier (excluded upstream
        // by the `el is FamilyInstance` check). Modeled here by the absence of a
        // point-based placement name.
        Assert.False(PlacementClassifier.IsPointBased(null));
    }

    [Fact]
    public void NotFeeding_means_no_routine_forms()
    {
        // Sanity: if the gate withholds a curve-based instance, nothing is fed and no
        // routine can form — even across repeated episodes.
        var detector = new RoutineDetector();
        var fired = false;
        detector.RoutineDetected += (_, _) => fired = true;

        // Records exist, but because the placement gate is false we simply never call Feed.
        const string beamPlacement = "CurveDrivenStructural";
        if (PlacementClassifier.IsPointBased(beamPlacement))
        {
            foreach (var (elemId, tagId) in new[] { (301, 401), (311, 411) })
            {
                detector.Feed(Place(elemId, "Structural Framing", "W-Beam"));
                detector.Feed(SetParam(elemId, "Mark"));
                detector.Feed(Tag(tagId, elemId, "Beam Tag"));
            }
        }

        Assert.False(fired);
    }
}
