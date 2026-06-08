using System.ComponentModel;

namespace RevitWriteServer.Chat;

/// <summary>
/// A single message in the chat history.
/// INotifyPropertyChanged lets WPF update the bubble text as streaming chunks arrive.
/// </summary>
public class ChatMessage : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;

    public bool IsUser { get; init; }

    private string _text = "";
    public string Text
    {
        get => _text;
        set
        {
            _text = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Text)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DisplayText)));
        }
    }

    /// <summary>Text shown in the bubble — control tokens stripped.</summary>
    public string DisplayText =>
        Text.Replace("##EXECUTE##", "").Replace("##DISMISS##", "").Trim();
}
