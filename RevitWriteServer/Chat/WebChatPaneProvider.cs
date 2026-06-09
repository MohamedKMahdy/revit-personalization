using Autodesk.Revit.UI;

namespace RevitWriteServer.Chat;

/// <summary>
/// Registers the WebView2 chat panel as a Revit dockable pane.
/// Must be registered in IExternalApplication.OnStartup.
///
/// Static references (Panel, DockablePane) are populated by App.cs after
/// ApplicationInitialized fires so that NotifyPatternCommand and the ribbon
/// button can show and navigate the pane without holding a UIApplication ref.
/// </summary>
public class WebChatPaneProvider : IDockablePaneProvider
{
    /// <summary>Stable GUID — must never change between versions.</summary>
    public static readonly DockablePaneId PanelId =
        new(new Guid("D2E3F4A5-B6C7-8901-BCDE-F12345678901"));

    // ── Static refs populated by App.cs after initialisation ─────────────────

    /// <summary>The live WebView2 panel instance.</summary>
    public static WebChatPanel? Panel { get; set; }

    /// <summary>
    /// The Revit DockablePane handle — used to show/focus the pane.
    /// Populated in App.OnApplicationInitialized via uiApp.GetDockablePane().
    /// </summary>
    public static DockablePane? Pane { get; set; }

    // ── IDockablePaneProvider ─────────────────────────────────────────────────

    public void SetupDockablePane(DockablePaneProviderData data)
    {
        var panel = new WebChatPanel();
        Panel = panel;

        data.FrameworkElement = panel;
        data.InitialState = new DockablePaneState
        {
            DockPosition = DockPosition.Right,
            MinimumWidth = 360,
        };
        data.VisibleByDefault = false;  // only shown when a pattern fires or user clicks the button
    }
}
