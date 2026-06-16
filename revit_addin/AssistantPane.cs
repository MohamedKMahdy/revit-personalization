using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Autodesk.Revit.UI;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace RevitLogger;

/// <summary>
/// WPF content for the "BIM Assistant" dockable pane. Hosts an embedded WebView2
/// pointed at the local chatbot server (http://127.0.0.1:5000) so the assistant
/// lives inside Revit instead of an external browser.
///
/// Lifecycle notes:
///   • Built once at OnStartup on the Revit UI thread; the SAME instance is reused
///     for the life of the session (Revit requires a stable FrameworkElement).
///   • WebView2 needs a WRITABLE user-data folder. Revit runs from Program Files,
///     so the default would throw — we point it at %LOCALAPPDATA%\RevitPersonalization.
///   • CoreWebView2 init is async and is deferred to the Loaded event (first time
///     the pane is shown), so it never slows Revit startup.
/// </summary>
internal sealed class AssistantPane : UserControl, IDockablePaneProvider
{
    public static readonly DockablePaneId PaneId =
        new(new Guid("b2f4c7a1-3d6e-49a8-9c10-77e2a4d8f001"));

    private readonly WebView2 _web;
    private readonly TextBlock _statusText;
    private readonly Border _overlay;
    private bool _initStarted;          // OnLoaded ran once
    private bool _coreReady;            // CoreWebView2 initialised
    private bool _disposed;             // WebView torn down (Revit shutting down)
    private int _navAttempts;
    private DispatcherTimer? _retryTimer;   // single pending retry (never orphaned)
    private const int MaxNavAttempts = 6;

    public AssistantPane()
    {
        _web = new WebView2();
        _web.NavigationCompleted += OnNavigationCompleted;

        _statusText = new TextBlock
        {
            Text = "Starting BIM Assistant…",
            Foreground = Brushes.White,
            FontSize = 13,
            TextAlignment = TextAlignment.Center,
            TextWrapping = TextWrapping.Wrap,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(24),
        };

        _overlay = new Border
        {
            Background = new SolidColorBrush(Color.FromRgb(0x20, 0x24, 0x2b)),
            Child = _statusText,
        };

        var grid = new Grid();
        grid.Children.Add(_web);
        grid.Children.Add(_overlay);   // overlay on top until first successful load
        Content = grid;

        Loaded += OnLoaded;
    }

    // ── IDockablePaneProvider ────────────────────────────────────────────────
    public void SetupDockablePane(DockablePaneProviderData data)
    {
        data.FrameworkElement = this;
        data.InitialState = new DockablePaneState
        {
            DockPosition = DockPosition.Right,
        };
    }

    // ── Init / navigation ────────────────────────────────────────────────────
    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (_initStarted) return;     // Loaded can fire again on re-dock; init once
        _initStarted = true;

        try
        {
            var userData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RevitPersonalization", "WebView2");
            Directory.CreateDirectory(userData);

            var env = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: userData,
                options: null);
            await _web.EnsureCoreWebView2Async(env);
            _coreReady = true;

            SetStatus("Starting the assistant server…");
            // Start the server off the UI thread; await resumes on the UI thread.
            await System.Threading.Tasks.Task.Run(() => ChatServer.EnsureRunning());
            _navAttempts = 0;
            Navigate();                       // the one and only initial navigation
        }
        catch (Exception ex)
        {
            App.DiagLog($"AssistantPane init failed: {ex}");
            SetStatus($"Could not start the embedded browser:\n{ex.Message}\n\n"
                    + "The assistant is still available in a normal browser at " + ChatServer.Url);
        }
    }

    private void Navigate()
    {
        if (_disposed || _web.CoreWebView2 is null) return;
        _navAttempts++;
        try { _web.CoreWebView2.Navigate(ChatServer.Url); }
        catch (Exception ex)
        {
            App.DiagLog($"AssistantPane navigate failed: {ex.Message}");
            SetStatus($"Navigation error: {ex.Message}");
        }
    }

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        if (e.IsSuccess)
        {
            _overlay.Visibility = Visibility.Collapsed;   // reveal the page
            return;
        }

        // Server may still be booting — retry a few times with a short delay.
        if (!_disposed && _navAttempts < MaxNavAttempts)
        {
            SetStatus($"Waiting for the assistant server… (attempt {_navAttempts}/{MaxNavAttempts})");
            ScheduleRetry();
        }
        else if (!_disposed)
        {
            SetStatus("The assistant server did not respond.\n"
                    + "Make sure setup_revit_env.py has been run, then click Open Assistant again.\n\n"
                    + "You can also open " + ChatServer.Url + " in a browser.");
        }
    }

    /// <summary>Arms a single pending retry, replacing any previous one (no orphaned timers).</summary>
    private void ScheduleRetry()
    {
        _retryTimer?.Stop();
        _retryTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1.5) };
        _retryTimer.Tick += (_, _) =>
        {
            _retryTimer!.Stop();
            if (!_disposed) Navigate();
        };
        _retryTimer.Start();
    }

    private void SetStatus(string msg)
    {
        _overlay.Visibility = Visibility.Visible;
        _statusText.Text = msg;
    }

    /// <summary>
    /// Force a fresh navigation when the user clicks the ribbon button again. No-op while
    /// the WebView2 is still initialising — OnLoaded drives the first navigation.
    /// </summary>
    public async void Reload()
    {
        if (_disposed || !_coreReady) return;   // first nav is owned by OnLoaded
        _retryTimer?.Stop();                     // cancel any in-flight retry chain
        _navAttempts = 0;
        SetStatus("Reloading…");
        try
        {
            await System.Threading.Tasks.Task.Run(() => ChatServer.EnsureRunning());
            if (!_disposed) Navigate();          // resumes on the UI thread
        }
        catch (Exception ex)
        {
            App.DiagLog($"AssistantPane reload failed: {ex}");
            SetStatus($"Could not reload the assistant: {ex.Message}");
        }
    }

    public void DisposeWebView()
    {
        _disposed = true;
        try { _retryTimer?.Stop(); } catch { /* ignore on shutdown */ }
        try { _web.Dispose(); } catch { /* ignore on shutdown */ }
    }
}
