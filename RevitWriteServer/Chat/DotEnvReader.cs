using System.IO;

namespace RevitWriteServer.Chat;

/// <summary>
/// Reads ANTHROPIC_API_KEY from:
///   1. Windows environment variable  (set system-wide — preferred for Revit)
///   2. .env file walked up from the assembly's directory
///
/// To set the key system-wide (run once in PowerShell as admin):
///   [System.Environment]::SetEnvironmentVariable(
///       "ANTHROPIC_API_KEY", "sk-ant-...", "User")
/// Then restart Revit so the new env var is visible.
/// </summary>
public static class DotEnvReader
{
    public static string GetApiKey(string variable = "ANTHROPIC_API_KEY")
    {
        // 1. Environment variable (fastest path)
        var fromEnv = Environment.GetEnvironmentVariable(variable);
        if (!string.IsNullOrWhiteSpace(fromEnv)) return fromEnv;

        // 2. Walk up from the assembly location looking for a .env file
        var dir = new DirectoryInfo(
            Path.GetDirectoryName(typeof(DotEnvReader).Assembly.Location) ?? ".");

        for (int i = 0; i < 8 && dir is not null; i++, dir = dir.Parent)
        {
            var path = Path.Combine(dir.FullName, ".env");
            if (!File.Exists(path)) continue;
            foreach (var line in File.ReadLines(path))
            {
                var t = line.Trim();
                if (!t.StartsWith(variable + "=", StringComparison.OrdinalIgnoreCase)) continue;
                return t[(variable.Length + 1)..].Trim().Trim('"').Trim('\'');
            }
        }
        return string.Empty;
    }
}
