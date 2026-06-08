using Autodesk.Revit.UI;

namespace RevitWriteServer.Chat;

/// <summary>
/// Registers the chat panel as a Revit dockable pane.
/// Must be registered in IExternalApplication.OnStartup.
/// </summary>
public class ChatPaneProvider : IDockablePaneProvider
{
    /// <summary>Stable GUID — must never change between versions.</summary>
    public static readonly DockablePaneId PanelId =
        new(new Guid("C1D2E3F4-A5B6-7890-ABCD-EF1234567892"));

    private ChatPanel? _panel;

    /// <summary>The live panel instance — available after SetupDockablePane is called.</summary>
    public ChatPanel? Panel => _panel;

    public void SetupDockablePane(DockablePaneProviderData data)
    {
        _panel = new ChatPanel();
        data.FrameworkElement = _panel;
        data.InitialState = new DockablePaneState
        {
            DockPosition    = DockPosition.Right,
            MinimumWidth    = 320,
        };
        data.VisibleByDefault = true;   // always visible — user can dock/undock freely
    }
}
