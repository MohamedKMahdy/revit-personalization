using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;

namespace RevitWriteServer.Chat;

/// <summary>
/// Dockable pane content — a Chromium (WebView2) control that renders the
/// Python chatbot server at http://localhost:5000 directly inside Revit.
///
/// Lifecycle:
///   1. Revit creates the pane and calls SetupDockablePane → WebChatPanel is
///      constructed (WebView2 not yet initialised — it needs a window handle).
///   2. When the control is first loaded into the visual tree (Loaded event),
///      WebView2 is asynchronously initialised with a persistent user-data
///      folder under %LOCALAPPDATA%\RevitPersonalization\WebView2.
///   3. NavigateTo(url) can be called at any time from any thread; if WebView2
///      isn't ready yet it waits.  Calls from background threads go through
///      Dispatcher so the CoreWebView2 API is always called on the UI thread.
/// </summary>
public partial class WebChatPanel : UserControl
{
    // Completed when CoreWebView2 is ready (or faulted on permanent failure)
    private readonly TaskCompletionSource<bool> _ready = new();

    public WebChatPanel()
    {
        InitializeComponent();
        Loaded += OnLoaded;
    }

    // ── Initialisation ────────────────────────────────────────────────────────

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        // Only initialise once (Loaded can fire again after dock/undock)
        if (_ready.Task.IsCompleted) return;

        try
        {
            SetSplash("Starting BIM Assistant…");

            // Use a persistent user-data folder so cookies / session survive
            // Revit restarts.  Do NOT use the Revit install directory (no write access).
            var cacheDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RevitPersonalization", "WebView2");

            var env = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: cacheDir);

            await WebView.EnsureCoreWebView2Async(env);

            // Suppress right-click context menu and dev-tools shortcut
            WebView.CoreWebView2.Settings.AreDefaultContextMenusEnabled  = false;
            WebView.CoreWebView2.Settings.AreDevToolsEnabled             = false;

            // Navigate to the waiting-room page until a real pattern arrives
            WebView.CoreWebView2.Navigate("about:blank");

            WebView.Visibility  = Visibility.Visible;
            Splash.Visibility   = Visibility.Collapsed;

            _ready.TrySetResult(true);
        }
        catch (Exception ex)
        {
            // WebView2 Runtime not installed, or other fatal error
            SetSplash(
                "⚠ WebView2 could not start.\n\n" +
                $"{ex.Message}\n\n" +
                "Install Microsoft Edge WebView2 Runtime, then restart Revit.\n\n" +
                "You can still use the chatbot by opening this URL in a browser:");
            UrlHint.Visibility = Visibility.Visible;
            _ready.TrySetException(ex);
        }
    }

    // ── Public API ────────────────────────────────────────────────────────────

    /// <summary>
    /// Navigate the embedded browser to <paramref name="url"/>.
    /// Safe to call from any thread; awaits WebView2 initialisation automatically.
    /// </summary>
    public void NavigateTo(string url)
    {
        _ = Dispatcher.InvokeAsync(async () =>
        {
            try
            {
                if (!_ready.Task.IsCompleted)
                    SetSplash("Starting BIM Assistant…");

                await _ready.Task;

                // If already on this URL, force a reload so the browser
                // picks up any server-side changes (bypasses cache).
                var current = WebView.Source?.ToString() ?? "";
                if (current.TrimEnd('/') == url.TrimEnd('/'))
                    WebView.CoreWebView2.Reload();
                else
                    WebView.CoreWebView2.Navigate(url);
            }
            catch
            {
                SetSplash("Open in your browser:");
                UrlHint.Text       = url;
                UrlHint.Visibility = Visibility.Visible;
            }
        });
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private void SetSplash(string text)
    {
        SplashText.Text       = text;
        Splash.Visibility     = Visibility.Visible;
        WebView.Visibility    = Visibility.Collapsed;
    }
}
