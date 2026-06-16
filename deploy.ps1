# deploy.ps1 — copy the built add-in into the Revit add-ins folder.
# Run AFTER closing Revit (the DLL is locked while Revit is open).
#
# Layout after deploy:
#   %APPDATA%\Autodesk\Revit\Addins\<version>\
#       RevitLogger.addin          ← manifest (points to RevitLogger\RevitLogger.dll)
#       RevitLogger\
#           RevitLogger.dll
#           RevitLogger.deps.json  ← current dependency manifest
#           WebView2Loader.dll     ← native loader (hardening; Revit also ships one)
#
# The add-in uses System.Text.Json (in-box) — no Newtonsoft. WebView2 managed
# assemblies are loaded from the Revit install (Private=false), not deployed here.
#
# Supported: Revit 2025 and 2026 (both .NET 8 / net8.0-windows).
# Usage:
#   .\deploy.ps1                    # default: Revit 2026
#   .\deploy.ps1 -RevitVersion 2025 # Revit 2025

param([string]$RevitVersion = "2026")

$buildDir   = "$PSScriptRoot\revit_addin\bin\Release\net8.0-windows"
$revitDir   = "C:\Program Files\Autodesk\Revit $RevitVersion"
$addinsDir  = "$env:APPDATA\Autodesk\Revit\Addins\$RevitVersion"
$destSubDir = "$addinsDir\RevitLogger"

# Verify build output exists
if (-not (Test-Path "$buildDir\RevitLogger.dll")) {
    Write-Error "Build output not found: $buildDir\RevitLogger.dll`nRun: dotnet build revit_addin\RevitLogger.csproj -c Release -p:RevitVersion=$RevitVersion"
    exit 1
}

# Ensure destination directories exist
New-Item -ItemType Directory -Force -Path $addinsDir  | Out-Null
New-Item -ItemType Directory -Force -Path $destSubDir | Out-Null

# Clean stale artifacts (old deps.json, leftover Newtonsoft.Json.dll, dll.config) so the
# deployed folder always matches the current build. Preserve the runtime Logs subfolder.
Get-ChildItem -Path $destSubDir -Force |
    Where-Object { $_.Name -ne 'Logs' } |
    Remove-Item -Force -Recurse -ErrorAction SilentlyContinue

# Copy manifest to the addins root
Copy-Item -Path "$PSScriptRoot\revit_addin\RevitLogger.addin" -Destination "$addinsDir\RevitLogger.addin" -Force

# Copy DLL + current dependency manifest into the RevitLogger subfolder
$filesToCopy = @("RevitLogger.dll", "RevitLogger.pdb", "RevitLogger.deps.json")
foreach ($f in $filesToCopy) {
    $src = "$buildDir\$f"
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination "$destSubDir\$f" -Force
        Write-Host "  Copied: $f" -ForegroundColor Cyan
    }
}

# Native WebView2 loader next to the add-in (hardening — Revit's own copy normally
# satisfies the P/Invoke, but this makes the embedded pane self-contained).
$loader = "$revitDir\WebView2Loader.dll"
if (Test-Path $loader) {
    Copy-Item -Path $loader -Destination "$destSubDir\WebView2Loader.dll" -Force
    Write-Host "  Copied: WebView2Loader.dll (from Revit $RevitVersion)" -ForegroundColor Cyan
} else {
    Write-Host "  Note: WebView2Loader.dll not found in $revitDir (pane relies on Revit's own copy)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Deployed to: $addinsDir" -ForegroundColor Green
Write-Host "Now start Revit $RevitVersion and look for the 'BIM Personalization' tab → 'Open Assistant'."
Write-Host "Logs: $env:LOCALAPPDATA\RevitPersonalization\logs\"
