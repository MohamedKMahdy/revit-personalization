using System;
using System.Windows;
using System.Windows.Threading;

namespace RevitLogger;

/// <summary>
/// Non-modal WPF toast window shown when RoutineDetector confirms a repeated
/// Custom Element Instantiation pattern.
///
/// Lifecycle:
///   • Shown via App.OnRoutineDetected — always on the Revit UI thread (safe).
///   • Owner is set to the Revit main window (floats above Revit, below others).
///   • Auto-closes after AutoCloseSecs if the user takes no action.
///
/// "Learn as Shortcut" — calls the _onLearn delegate (App.LaunchOrchestrator),
/// which starts the Python agent pipeline in a new console window.
///
/// "Dismiss" — closes the notification without any action.
/// </summary>
public partial class NotificationUI : Window
{
    private const int AutoCloseSecs = 30;

    private readonly string         _routineId;
    private readonly Action<string> _onLearn;
    private readonly DispatcherTimer _timer;

    // ── Constructor ───────────────────────────────────────────────────────────

    public NotificationUI(string routineId, string title, string label,
                          Action<string> onLearn)
    {
        InitializeComponent();

        _routineId = routineId;
        _onLearn   = onLearn;

        TitleText.Text = title;
        LabelText.Text = label;

        // Position at bottom-right of the primary work area
        PositionBottomRight();

        // Auto-close timer
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(AutoCloseSecs) };
        _timer.Tick += (_, _) => Close();
        _timer.Start();
    }

    // ── Event handlers ────────────────────────────────────────────────────────

    private void OnLearn(object sender, RoutedEventArgs e)
    {
        _timer.Stop();
        try   { _onLearn(_routineId); }
        catch (Exception ex) { App.DiagLog($"NotificationUI.OnLearn delegate error: {ex.Message}"); }
        Close();
    }

    private void OnDismiss(object sender, RoutedEventArgs e)
    {
        _timer.Stop();
        Close();
    }

    protected override void OnClosed(EventArgs e)
    {
        _timer.Stop();
        base.OnClosed(e);
    }

    // ── Positioning ───────────────────────────────────────────────────────────

    private void PositionBottomRight()
    {
        try
        {
            var area = SystemParameters.WorkArea;
            Left = area.Right  - Width  - 24;
            Top  = area.Bottom - Height - 64;
        }
        catch
        {
            // Fallback: let Windows place the window
        }
    }
}
