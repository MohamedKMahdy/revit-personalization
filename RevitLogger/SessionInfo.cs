using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Serialization;
using Autodesk.Revit.DB;

namespace RevitLogger;

/// <summary>
/// Written as the first line of every .jsonl session file.
/// Provides metadata for reproducibility analysis (Jang &amp; Lee 2023).
/// The document path is SHA-1 hashed to protect IP/privacy (thesis §3.1 gap 3).
/// </summary>
public class SessionInfo
{
    [JsonPropertyName("record_type")]
    public string RecordType { get; } = "session_start";

    [JsonPropertyName("schema_version")]
    public string SchemaVersion { get; } = "2.0";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    [JsonPropertyName("timestamp_utc")]
    public string TimestampUtc { get; set; } = "";

    [JsonPropertyName("revit_version")]
    public string RevitVersion { get; set; } = "";

    /// <summary>
    /// First 12 hex chars of SHA-1(doc.PathName.ToLower()).
    /// Identifies the project without exposing the file-system path.
    /// </summary>
    [JsonPropertyName("document_hash")]
    public string DocumentHash { get; set; } = "";

    [JsonPropertyName("document_title")]
    public string DocumentTitle { get; set; } = "";

    public static SessionInfo Create(Document doc, string sessionId, string revitVersion)
    {
        var pathHash = string.IsNullOrEmpty(doc.PathName)
            ? "unsaved"
            : HashPath(doc.PathName);

        return new SessionInfo
        {
            SessionId      = sessionId,
            TimestampUtc   = DateTime.UtcNow.ToString("o"),
            RevitVersion   = revitVersion,
            DocumentHash   = pathHash,
            DocumentTitle  = doc.Title,
        };
    }

    private static string HashPath(string path)
    {
        var bytes = Encoding.UTF8.GetBytes(path.ToLowerInvariant());
        var hash  = SHA1.HashData(bytes);
        return Convert.ToHexString(hash)[..12].ToLower();
    }
}
