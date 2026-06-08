using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using RevitWriteServer.Commands;

namespace RevitWriteServer;

/// <summary>
/// Listens on localhost:8080 (same port as mcp-servers-for-revit) and dispatches
/// incoming JSON-RPC 2.0 requests to registered IRevitCommand implementations.
///
/// One connection per request — the client writes a single JSON object and reads
/// back a single JSON object, then closes. This mirrors the mcp-servers-for-revit
/// TCP protocol exactly so the Python revit_bridge.py works against both.
///
/// Protocol:
///   Request:  {"jsonrpc":"2.0","method":"say_hello","params":{},"id":"1"}
///   Response: {"jsonrpc":"2.0","result":{...},"id":"1"}
///          or {"jsonrpc":"2.0","error":{"code":-32000,"message":"..."},"id":"1"}
/// </summary>
public class TcpCommandServer : IDisposable
{
    private const int Port = 8080;
    private readonly Dictionary<string, IRevitCommand> _commands = new(StringComparer.OrdinalIgnoreCase);
    private TcpListener? _listener;
    private CancellationTokenSource? _cts;
    private Task? _listenTask;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false
    };

    public void RegisterCommand(IRevitCommand command)
    {
        _commands[command.CommandName] = command;
    }

    public void Start()
    {
        _listener = new TcpListener(IPAddress.Loopback, Port);
        _listener.Start();
        _cts = new CancellationTokenSource();
        _listenTask = Task.Run(() => AcceptLoop(_cts.Token));
    }

    public void Stop()
    {
        _cts?.Cancel();
        _listener?.Stop();
        try { _listenTask?.Wait(TimeSpan.FromSeconds(3)); } catch { /* intentionally ignored */ }
    }

    private async Task AcceptLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener!.AcceptTcpClientAsync(ct);
            }
            catch (OperationCanceledException) { break; }
            catch { break; }

            // Handle each client on a thread-pool thread (fire and forget)
            _ = Task.Run(() => HandleClient(client), CancellationToken.None);
        }
    }

    private async Task HandleClient(TcpClient client)
    {
        using var _ = client;
        using var stream = client.GetStream();

        string requestJson;
        try
        {
            requestJson = await ReadAllAsync(stream);
        }
        catch
        {
            return; // broken pipe
        }

        var responseJson = ProcessRequest(requestJson);

        try
        {
            var bytes = Encoding.UTF8.GetBytes(responseJson);
            await stream.WriteAsync(bytes);
            await stream.FlushAsync();
        }
        catch { /* client disconnected */ }
    }

    private string ProcessRequest(string requestJson)
    {
        string? requestId = null;
        try
        {
            var doc = JsonDocument.Parse(requestJson);
            var root = doc.RootElement;

            requestId = root.TryGetProperty("id", out var idProp)
                ? idProp.GetRawText().Trim('"')
                : "0";

            if (!root.TryGetProperty("method", out var methodProp))
                return ErrorResponse(requestId, -32600, "Invalid Request: missing method");

            var method = methodProp.GetString() ?? "";
            JsonNode? paramsNode = null;
            if (root.TryGetProperty("params", out var paramsProp))
                paramsNode = JsonNode.Parse(paramsProp.GetRawText());

            if (!_commands.TryGetValue(method, out var command))
                return ErrorResponse(requestId, -32601, $"Method not found: {method}");

            var result = command.Execute(paramsNode, requestId ?? "0");
            return SuccessResponse(requestId, result);
        }
        catch (JsonException je)
        {
            return ErrorResponse(requestId ?? "0", -32700, $"Parse error: {je.Message}");
        }
        catch (ArgumentException ae)
        {
            return ErrorResponse(requestId ?? "0", -32602, $"Invalid params: {ae.Message}");
        }
        catch (Exception ex)
        {
            return ErrorResponse(requestId ?? "0", -32000, ex.Message);
        }
    }

    private static string SuccessResponse(string? id, object result)
    {
        var response = new
        {
            jsonrpc = "2.0",
            result,
            id = id ?? "0"
        };
        return JsonSerializer.Serialize(response, JsonOpts);
    }

    private static string ErrorResponse(string? id, int code, string message)
    {
        var response = new
        {
            jsonrpc = "2.0",
            error = new { code, message },
            id = id ?? "0"
        };
        return JsonSerializer.Serialize(response, JsonOpts);
    }

    /// <summary>
    /// Reads all available bytes from a NetworkStream into a UTF-8 string.
    /// The client sends one complete JSON object then stops sending, so we read
    /// until DataAvailable is false or the connection closes.
    /// </summary>
    private static async Task<string> ReadAllAsync(NetworkStream stream)
    {
        var buf = new byte[65536];
        var sb = new StringBuilder();

        // Give the first bytes a moment to arrive
        await Task.Delay(10);

        do
        {
            while (stream.DataAvailable)
            {
                int n = await stream.ReadAsync(buf);
                if (n == 0) break;
                sb.Append(Encoding.UTF8.GetString(buf, 0, n));
            }

            // Validate — if we have a complete JSON object, stop reading
            var s = sb.ToString().Trim();
            if (s.Length > 0)
            {
                try { JsonDocument.Parse(s); return s; } catch { /* incomplete */ }
            }

            await Task.Delay(20);
        } while (stream.DataAvailable || sb.Length == 0);

        return sb.ToString();
    }

    public void Dispose() => Stop();
}
