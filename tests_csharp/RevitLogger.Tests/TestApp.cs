namespace RevitLogger;

/// <summary>
/// Test-only stub for App.DiagLog. RoutineDetector calls App.DiagLog for diagnostics;
/// the real App (App.cs) is an IExternalApplication that references RevitAPI, so we do
/// not compile it into the test assembly. This no-op stub satisfies the reference.
/// </summary>
internal static class App
{
    internal static void DiagLog(string message) { /* no-op in tests */ }
}
