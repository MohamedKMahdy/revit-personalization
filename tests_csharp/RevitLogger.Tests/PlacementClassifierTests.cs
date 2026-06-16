using RevitLogger;
using Xunit;

namespace RevitLogger.Tests;

/// <summary>
/// Verifies the FEED gate: only point-based FamilyPlacementType members feed the detector.
/// Member names are the exact Autodesk.Revit.DB.FamilyPlacementType values verified against
/// Revit 2026's RevitAPI.dll.
/// </summary>
public class PlacementClassifierTests
{
    [Theory]
    [InlineData("OneLevelBased")]        // furniture, casework, generic models, planting…
    [InlineData("OneLevelBasedHosted")]  // doors, windows, hosted fixtures
    [InlineData("TwoLevelsBased")]       // columns
    [InlineData("WorkPlaneBased")]       // face-based families
    [InlineData("ViewBased")]            // detail components (included per decision)
    public void PointBased_placements_feed(string placementName)
    {
        Assert.True(PlacementClassifier.IsPointBased(placementName));
    }

    [Theory]
    [InlineData("Adaptive")]               // multi-point — no single insertion point
    [InlineData("CurveBased")]             // line-based
    [InlineData("CurveBasedDetail")]       // line-based detail
    [InlineData("CurveDrivenStructural")]  // beams / braces / slanted columns
    [InlineData("Invalid")]                // sentinel
    [InlineData("NotARealPlacementType")]  // unknown name
    [InlineData("")]
    [InlineData(null)]
    public void NonPointBased_placements_do_not_feed(string? placementName)
    {
        Assert.False(PlacementClassifier.IsPointBased(placementName));
    }
}
