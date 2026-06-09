using System.IO;

namespace RevitWriteServer.Chat;

/// <summary>
/// Reads ANTHROPIC_API_KEY from (in order):
///   1. Windows environment variable  — set once, works for all processes
///   2. %LOCALAPPDATA%\RevitPersonalization\.env  — populated by setup_revit_env.py
///   3. ~/revit-personalization/.env  — project folder
///   4. Walk up from assembly location  — catches dev builds
///
/// Quickest setup — run once from the project root:
///   python setup_revit_env.py
/// Then restart Revit.
/// </summary>
public static class DotEnvReader
{
    public static string GetApiKey(string variable = "ANTHROPIC_API_KEY")
    {
        // 1. Windows environment variable
        var fromEnv = Environment.GetEnvironmentVariable(variable);
        if (!string.IsNullOrWhiteSpace(fromEnv)) return fromEnv;

        // 2. Well-known drop location written by setup_revit_env.py
        var knownPaths = new[]
        {
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RevitPersonalization", ".env"),
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "revit-personalization", ".env"),
        };
        foreach (var kp in knownPaths)
        {
            var v = TryRead(kp, variable);
            if (!string.IsNullOrWhiteSpace(v)) return v;
        }

        // 3. Walk up from the assembly location
        var dir = new DirectoryInfo(
            Path.GetDirectoryName(typeof(DotEnvReader).Assembly.Location) ?? ".");
        for (int i = 0; i < 8 && dir is not null; i++, dir = dir.Parent)
        {
            var v = TryRead(Path.Combine(dir.FullName, ".env"), variable);
            if (!string.IsNullOrWhiteSpace(v)) return v;
        }

        return string.Empty;
    }

    private static string TryRead(string path, string variable)
    {
        if (!File.Exists(path)) return string.Empty;
        foreach (var line in File.ReadLines(path))
        {
            var t = line.Trim();
            if (!t.StartsWith(variable + "=", StringComparison.OrdinalIgnoreCase)) continue;
            return t[(variable.Length + 1)..].Trim().Trim('"').Trim('\'');
        }
        return string.Empty;
    }
}
