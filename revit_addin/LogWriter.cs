using System.Collections.Concurrent;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace RevitLogger;

/// <summary>
/// Async JSONL (JSON Lines) writer — one JSON object per line.
///
/// File location: %LOCALAPPDATA%\RevitPersonalization\logs\
/// File name:     session_YYYYMMDD_HHmmss_&lt;docHash&gt;.jsonl
///
/// Line 1:  SessionInfo  (record_type = "session_start")
/// Lines 2+: ActionRecord objects
/// Last line: session_end marker
///
/// Uses a BlockingCollection so the Revit UI thread never blocks on I/O.
/// Serialised with System.Text.Json (built into .NET 8, no NuGet dependency).
/// If the write loop crashes, the exception is written to a sidecar .error.txt file.
/// </summary>
public class LogWriter : IDisposable
{
    public string SessionId { get; }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented           = false,
        // Ensure enum values are written as strings (e.g. "Model", not 0)
        Converters              = { new JsonStringEnumConverter() },
    };

    // UTF-8 without BOM — standard for JSONL/JSON files
    private static readonly Encoding Utf8NoBom = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);

    private readonly BlockingCollection<object> _queue = new(boundedCapacity: 2000);
    private readonly Task   _writeTask;
    private readonly CancellationTokenSource _cts = new();
    private readonly string _filePath;
    private readonly string _errorPath;

    public LogWriter(string sessionId, string docHash)
    {
        SessionId = sessionId;

        var logDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "RevitPersonalization", "logs");
        Directory.CreateDirectory(logDir);

        _filePath  = Path.Combine(logDir,
            $"session_{DateTime.UtcNow:yyyyMMdd_HHmmss}_{docHash}.jsonl");
        _errorPath = _filePath + ".error.txt";

        App.DiagLog($"LogWriter created: {_filePath}");
        _writeTask = Task.Run(WriteLoop);
    }

    public void WriteSessionStart(SessionInfo info)
    {
        var added = _queue.TryAdd(info);
        App.DiagLog($"WriteSessionStart: TryAdd returned {added}, queue count={_queue.Count}");
    }

    public void Enqueue(ActionRecord record)
    {
        _queue.TryAdd(record);
    }

    public void Flush()
    {
        App.DiagLog($"LogWriter.Flush called, queue count={_queue.Count}");
        _queue.TryAdd(new SessionEnd(SessionId));
        _cts.CancelAfter(TimeSpan.FromSeconds(5));
        try { _writeTask.Wait(TimeSpan.FromSeconds(6)); } catch { }

        if (_writeTask.IsFaulted)
            App.DiagLog($"WriteTask faulted: {_writeTask.Exception?.Flatten().InnerException?.Message}");
        else
            App.DiagLog($"WriteTask status: {_writeTask.Status}");
    }

    public void Dispose() => Flush();

    private async Task WriteLoop()
    {
        App.DiagLog($"WriteLoop started, file={_filePath}");
        try
        {
            await using var sw = new StreamWriter(_filePath, append: false, Utf8NoBom);
            App.DiagLog("WriteLoop: StreamWriter opened");

            try
            {
                foreach (var item in _queue.GetConsumingEnumerable(_cts.Token))
                {
                    App.DiagLog($"WriteLoop: dequeued item type={item.GetType().Name}");
                    await WriteItem(sw, item);
                    App.DiagLog("WriteLoop: item written and flushed");
                }
            }
            catch (OperationCanceledException)
            {
                App.DiagLog("WriteLoop: cancellation requested, draining remaining items");
                while (_queue.TryTake(out var item))
                    await WriteItem(sw, item);
            }

            App.DiagLog("WriteLoop: loop exited normally");
        }
        catch (Exception ex)
        {
            // Write the exception to a sidecar file so it's visible even if Revit's
            // journal is not inspected.
            var msg = $"[{DateTime.UtcNow:o}] WriteLoop CRASHED:\n{ex}\n";
            App.DiagLog($"WriteLoop CRASHED: {ex.Message}");
            try { File.WriteAllText(_errorPath, msg); } catch { }
        }
    }

    private static async Task WriteItem(StreamWriter sw, object item)
    {
        string line;
        try
        {
            line = item switch
            {
                SessionInfo si  => JsonSerializer.Serialize(si,  JsonOpts),
                ActionRecord ar => JsonSerializer.Serialize(ar,  JsonOpts),
                SessionEnd se   => JsonSerializer.Serialize(se,  JsonOpts),
                _               => JsonSerializer.Serialize(item, JsonOpts),
            };
        }
        catch (Exception ex)
        {
            // Serialization failure — write a comment line so the file is not empty
            line = $"{{\"_error\":\"serialize failed: {ex.Message.Replace("\"","'")}\"}}";
            App.DiagLog($"WriteItem serialize error: {ex.Message}");
        }

        await sw.WriteLineAsync(line);
        await sw.FlushAsync();
    }

    private sealed record SessionEnd(
        [property: JsonPropertyName("record_type")]   string RecordType,
        [property: JsonPropertyName("session_id")]    string SessionId,
        [property: JsonPropertyName("timestamp_utc")] string TimestampUtc)
    {
        public SessionEnd(string sessionId)
            : this("session_end", sessionId, DateTime.UtcNow.ToString("o")) { }
    }
}
