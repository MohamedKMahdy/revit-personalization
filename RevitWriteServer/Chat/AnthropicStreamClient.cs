using System.IO;
using System.Net.Http;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;

namespace RevitWriteServer.Chat;

/// <summary>
/// Thin streaming client for the Anthropic Messages API.
/// Uses HttpClient + SSE — no NuGet package required.
///
/// SSE event format we care about:
///   event: content_block_delta
///   data: {"type":"content_block_delta","index":0,
///          "delta":{"type":"text_delta","text":"Hello"}}
/// </summary>
public class AnthropicStreamClient : IDisposable
{
    private readonly HttpClient _http = new();
    private const string ApiUrl = "https://api.anthropic.com/v1/messages";
    public const string Model = "claude-opus-4-8";

    public AnthropicStreamClient(string apiKey)
    {
        _http.DefaultRequestHeaders.Add("x-api-key", apiKey);
        _http.DefaultRequestHeaders.Add("anthropic-version", "2023-06-01");
    }

    /// <summary>
    /// Stream text chunks from Claude.
    /// <paramref name="messages"/> must alternate user/assistant, starting with user.
    /// </summary>
    public async IAsyncEnumerable<string> StreamAsync(
        string system,
        IList<(string role, string content)> messages,
        int maxTokens = 512,
        [EnumeratorCancellation] CancellationToken ct = default)
    {
        var body = JsonSerializer.Serialize(new
        {
            model      = Model,
            max_tokens = maxTokens,
            system,
            stream     = true,
            messages   = messages
                .Select(m => new { role = m.role, content = m.content })
                .ToArray(),
        });

        var req = new HttpRequestMessage(HttpMethod.Post, ApiUrl)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };

        using var resp = await _http.SendAsync(
            req, HttpCompletionOption.ResponseHeadersRead, ct);
        resp.EnsureSuccessStatusCode();

        using var stream = await resp.Content.ReadAsStreamAsync(ct);
        using var reader = new StreamReader(stream);

        while (!reader.EndOfStream && !ct.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync();
            if (string.IsNullOrWhiteSpace(line) || !line.StartsWith("data: ")) continue;

            var data = line[6..];
            if (data is "[DONE]") break;

            JsonDocument? doc = null;
            try { doc = JsonDocument.Parse(data); }
            catch { continue; }

            using (doc)
            {
                if (!doc.RootElement.TryGetProperty("type", out var t)) continue;
                if (t.GetString() != "content_block_delta") continue;
                if (!doc.RootElement.TryGetProperty("delta", out var delta)) continue;
                if (delta.TryGetProperty("type", out var dt) &&
                    dt.GetString() != "text_delta") continue;
                if (delta.TryGetProperty("text", out var textProp))
                {
                    var chunk = textProp.GetString();
                    if (!string.IsNullOrEmpty(chunk)) yield return chunk;
                }
            }
        }
    }

    public void Dispose() => _http.Dispose();
}
