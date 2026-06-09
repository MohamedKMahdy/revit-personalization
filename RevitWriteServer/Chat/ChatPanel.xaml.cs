using System.Collections.ObjectModel;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace RevitWriteServer.Chat;

/// <summary>
/// WPF dockable panel — Claude chatbot running inside Revit.
///
/// Flow:
///   1. NotifyPatternCommand calls LoadPattern() on the UI thread
///   2. Panel shows the pattern info and starts the Claude greeting via SSE
///   3. User types, clicks Execute/Dismiss, or just says "yes"
///   4. On ##EXECUTE## token or Execute button → ExecuteCallback fires (UI thread → Revit API)
/// </summary>
public partial class ChatPanel : UserControl
{
    private readonly ObservableCollection<ChatMessage> _messages = new();
    private readonly List<(string role, string content)> _history = new();
    private AnthropicStreamClient? _claude;
    private CancellationTokenSource? _cts;

    private bool _busy;
    private bool _done;

    // Set by NotifyPatternCommand — async: raises ExternalEvent and awaits API-context completion
    private Func<Task>? _executeCallback;

    // ── Construction ──────────────────────────────────────────────────────────

    public ChatPanel()
    {
        InitializeComponent();
        MsgList.ItemsSource = _messages;

        var apiKey = DotEnvReader.GetApiKey();
        if (!string.IsNullOrWhiteSpace(apiKey))
            _claude = new AnthropicStreamClient(apiKey);

        ShowIdleState();
    }

    private void ShowIdleState()
    {
        TitleText.Text = "BIM Assistant";
        MetaText.Text  = "Watching for repeated patterns…";

        BtnTest.Visibility    = Visibility.Visible;
        BtnExecute.Visibility = Visibility.Collapsed;
        BtnDismiss.Visibility = Visibility.Collapsed;
        BtnExecute.IsEnabled  = false;
        BtnDismiss.IsEnabled  = false;
        BtnSend.IsEnabled     = false;
        InputBox.IsEnabled    = false;
        StatusBar.Visibility  = Visibility.Collapsed;

        _messages.Clear();

        if (_claude is null)
        {
            _messages.Add(new ChatMessage
            {
                IsUser = false,
                Text   = "⚠️  ANTHROPIC_API_KEY not set.\n\n" +
                         "Set it as a Windows user environment variable, then restart Revit:\n\n" +
                         "[System.Environment]::SetEnvironmentVariable(\n" +
                         "  \"ANTHROPIC_API_KEY\", \"sk-ant-...\", \"User\")"
            });
        }
        else
        {
            _messages.Add(new ChatMessage
            {
                IsUser = false,
                Text   = "👋 Hello! I'm watching your modeling session.\n\n" +
                         "When I notice you repeating the same sequence of steps " +
                         "(placing a family, setting parameters, tagging), " +
                         "I'll pop up here and offer to save it as a one-click shortcut.\n\n" +
                         "Just keep modeling normally."
            });
        }
    }

    // ── Public API (called by NotifyPatternCommand on the UI thread) ──────────

    public void LoadPattern(PatternData data)
    {
        // Cancel any in-flight stream
        _cts?.Cancel();
        _cts = new CancellationTokenSource();

        // Reset state
        _messages.Clear();
        _history.Clear();
        _done  = false;
        _busy  = false;
        _executeCallback = data.ExecuteCallback;

        BtnTest.Visibility    = Visibility.Collapsed;
        BtnExecute.Visibility = Visibility.Visible;
        BtnDismiss.Visibility = Visibility.Visible;
        BtnExecute.IsEnabled  = true;
        BtnDismiss.IsEnabled  = true;
        BtnSend.IsEnabled     = true;
        InputBox.IsEnabled    = true;
        StatusBar.Visibility  = Visibility.Collapsed;

        // Update header
        TitleText.Text = $"🔍 {data.Label}";
        MetaText.Text  = $"Detected {data.Count}× · {(data.ToolSequence?.Count ?? 0)} steps";

        if (_claude is null)
        {
            AddBotMessage("⚠️  ANTHROPIC_API_KEY not set.\n\n" +
                          "Set it as a Windows user environment variable and restart Revit:\n" +
                          "[System.Environment]::SetEnvironmentVariable(" +
                          "\"ANTHROPIC_API_KEY\", \"sk-ant-...\", \"User\")");
            return;
        }

        // Kick off the opening greeting
        _ = Task.Run(() => StreamGreeting(data, _cts.Token));
    }

    // ── Streaming ─────────────────────────────────────────────────────────────

    private string BuildSystemPrompt(PatternData data)
    {
        var motifStr = data.Motif is not null
            ? JsonSerializer.Serialize(data.Motif, new JsonSerializerOptions { WriteIndented = true })
            : "{}";
        var seqStr = data.ToolSequence is not null
            ? JsonSerializer.Serialize(data.ToolSequence, new JsonSerializerOptions { WriteIndented = true })
            : "[]";

        return $"""
            You are a BIM Workflow Assistant embedded in Autodesk Revit.
            You detected a repeated modeling routine that the user performs manually.

            DETECTED ROUTINE
            ================
            Name:       {data.Label}
            Repetitions: {data.Count}×

            MOTIF (structured):
            {motifStr}

            EXECUTION STEPS (what will run in Revit):
            {seqStr}

            RULES
            =====
            - Keep every response SHORT (2–4 sentences).
            - When the user confirms (yes / go / execute / do it / confirm / sure),
              output exactly ##EXECUTE## on its own line.
            - When the user declines (no / dismiss / cancel / not now),
              output exactly ##DISMISS## on its own line.
            - If the user wants to change a parameter, acknowledge it warmly.
            - Be friendly and concise — you are saving a BIM professional time.
            """;
    }

    private string _systemPrompt = "";

    private async Task StreamGreeting(PatternData data, CancellationToken ct)
    {
        _systemPrompt = BuildSystemPrompt(data);

        // Hidden init turn — gives Claude context to write a greeting
        var initTurn = ("user",
            "INIT: Greet me, explain the routine you detected in 2–3 plain sentences, " +
            "and ask if I'd like to run it or have questions.");
        _history.Add(initTurn);

        await StreamFromClaude(ct);
    }

    private async Task StreamFromClaude(CancellationToken ct)
    {
        if (_claude is null) return;

        await Dispatcher.InvokeAsync(() =>
        {
            _busy = true;
            BtnSend.IsEnabled = false;
            InputBox.IsEnabled = false;
        });

        var msg = new ChatMessage { IsUser = false };
        await Dispatcher.InvokeAsync(() =>
        {
            _messages.Add(msg);
            ScrollToBottom();
        });

        string fullText = "";
        try
        {
            await foreach (var chunk in _claude.StreamAsync(_systemPrompt, _history, ct: ct))
            {
                fullText += chunk;
                var display = fullText.Replace("##EXECUTE##", "").Replace("##DISMISS##", "").Trim();
                await Dispatcher.InvokeAsync(() =>
                {
                    msg.Text = display;
                    ScrollToBottom();
                });
            }
        }
        catch (OperationCanceledException) { return; }
        catch (Exception ex)
        {
            fullText = $"Error talking to Claude: {ex.Message}\n\nCheck your API key and internet connection.";
            await Dispatcher.InvokeAsync(() => msg.Text = fullText);
            await Dispatcher.InvokeAsync(() =>
            {
                _busy = false;
                BtnSend.IsEnabled    = true;
                InputBox.IsEnabled   = true;
            });
            return;
        }

        _history.Add(("assistant", fullText));

        await Dispatcher.InvokeAsync(() =>
        {
            if (fullText.Contains("##EXECUTE##"))
                DoExecute();
            else if (fullText.Contains("##DISMISS##"))
                DoDismiss();
            else
            {
                _busy = false;
                BtnSend.IsEnabled  = true;
                InputBox.IsEnabled = true;
                InputBox.Focus();
            }
        });
    }

    // ── Actions ───────────────────────────────────────────────────────────────

    // async void is correct here — WPF event handler, exceptions shown in UI not propagated
    private async void DoExecute()
    {
        _done = true;
        BtnExecute.IsEnabled = false;
        BtnDismiss.IsEnabled = false;
        BtnSend.IsEnabled    = false;
        InputBox.IsEnabled   = false;

        ShowStatus("⟳ Executing in Revit…", "#D1ECF1", "#0C5460");

        try
        {
            if (_executeCallback is not null)
            {
                // await yields the WPF message loop so Revit can dispatch the
                // ExternalEvent. The continuation resumes on the WPF dispatcher
                // via the captured SynchronizationContext.
                await _executeCallback.Invoke();
            }
            ShowStatus("✓ Done — shortcut executed.", "#D4EDDA", "#155724");
            AddBotMessage("Done! The shortcut has been applied to your Revit model.");
        }
        catch (Exception ex)
        {
            ShowStatus($"✗ {ex.Message}", "#F8D7DA", "#721C24");
            AddBotMessage($"Something went wrong during execution: {ex.Message}");
        }
    }

    private void DoDismiss()
    {
        _done = true;
        BtnExecute.IsEnabled = false;
        BtnDismiss.IsEnabled = false;
        BtnSend.IsEnabled    = false;
        InputBox.IsEnabled   = false;
        ShowStatus("Dismissed.", "#FFF3CD", "#856404");
    }

    // ── UI helpers ────────────────────────────────────────────────────────────

    private void AddBotMessage(string text)
    {
        Dispatcher.BeginInvoke(() =>
        {
            _messages.Add(new ChatMessage { IsUser = false, Text = text });
            ScrollToBottom();
        });
    }

    private void ScrollToBottom() =>
        Dispatcher.BeginInvoke(() => Scroll.ScrollToBottom(), System.Windows.Threading.DispatcherPriority.Background);

    private void ShowStatus(string text, string bgHex, string fgHex)
    {
        StatusBar.Background  = new SolidColorBrush((Color)ColorConverter.ConvertFromString(bgHex)!);
        StatusText.Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString(fgHex)!);
        StatusText.Text = text;
        StatusBar.Visibility = Visibility.Visible;
    }

    // ── Event handlers ────────────────────────────────────────────────────────

    private void BtnTest_Click(object sender, RoutedEventArgs e)
    {
        // Load a hardcoded sample pattern so you can test the chat
        // without RevitLogger needing to detect a real pattern first.
        var motifJson = System.Text.Json.Nodes.JsonNode.Parse("""
            {
              "steps": [
                {"action": "Place",    "family_type": "M_Single-Flush : 900x2100mm"},
                {"action": "SetParam", "param_name": "FireRating",    "param_value": "60"},
                {"action": "SetParam", "param_name": "Mark",          "param_value": "D-101"},
                {"action": "SetParam", "param_name": "Width",         "param_value": "900"},
                {"action": "Tag",      "family_type": "Door Tag"}
              ]
            }
            """);

        var seqJson = System.Text.Json.Nodes.JsonNode.Parse("""
            [
              {"tool":"place_element",         "arguments":{"family_type":"M_Single-Flush","location":{"x":0,"y":0,"z":0}}},
              {"tool":"set_parameter",         "arguments":{"element_id":"{{last_element_id}}","parameter_name":"FireRating","value":"60"}},
              {"tool":"set_parameter",         "arguments":{"element_id":"{{last_element_id}}","parameter_name":"Mark","value":"D-101"}},
              {"tool":"set_parameter",         "arguments":{"element_id":"{{last_element_id}}","parameter_name":"Width","value":900}},
              {"tool":"create_annotation_tag", "arguments":{"element_id":"{{last_element_id}}","tag_family":"Door Tag"}}
            ]
            """)?.AsArray();

        var data = new PatternData
        {
            Label        = "Place Door + Set 4 Params + Tag  [SAMPLE]",
            Count        = 5,
            Motif        = motifJson,
            ToolSequence = seqJson,
            // No real ExecuteCallback in test mode — return a faulted task
            ExecuteCallback = () => Task.FromException(new InvalidOperationException(
                "This is a test pattern — open a Revit project first, " +
                "then trigger a real pattern via RevitLogger.")),
        };
        LoadPattern(data);
    }

    private void BtnExecute_Click(object sender, RoutedEventArgs e) => UserSays("Execute");
    private void BtnDismiss_Click(object sender, RoutedEventArgs e) => UserSays("Dismiss");
    private void BtnSend_Click(object sender, RoutedEventArgs e)    => SendFromInput();

    private void InputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Return && !_busy && !_done)
        {
            SendFromInput();
            e.Handled = true;
        }
    }

    private void SendFromInput()
    {
        var text = InputBox.Text.Trim();
        if (string.IsNullOrEmpty(text) || _busy || _done) return;
        InputBox.Clear();
        UserSays(text);
    }

    private void UserSays(string text)
    {
        if (_done || _busy) return;
        _messages.Add(new ChatMessage { IsUser = true, Text = text });
        _history.Add(("user", text));
        ScrollToBottom();
        _cts ??= new CancellationTokenSource();
        _ = Task.Run(() => StreamFromClaude(_cts.Token));
    }
}
