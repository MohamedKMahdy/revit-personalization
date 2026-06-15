# deploy.ps1 — copy the built add-in into the Revit add-ins folder.
# Run AFTER closing Revit (the DLL is locked while Revit is open).
#
# Layout after deploy:
#   %APPDATA%\Autodesk\Revit\Addins\<version>\
#       RevitLogger.addin          ← manifest (points to RevitLogger\RevitLogger.dll)
#       RevitLogger\
#           RevitLogger.dll
#           Newtonsoft.Json.dll    ← bundled dependency
#
# Supported: Revit 2025 and 2026 (both .NET 8 / net8.0-windows).
# Usage:
#   .\deploy.ps1                    # default: Revit 2026
#   .\deploy.ps1 -RevitVersion 2025 # Revit 2025

param([string]$RevitVersion = "2026")

$buildDir   = "$PSScriptRoot\revit_addin\bin\Release\net8.0-windows"
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

# Copy manifest to addins root (if not already there or changed)
Copy-Item -Path "$PSScriptRoot\revit_addin\RevitLogger.addin" -Destination "$addinsDir\RevitLogger.addin" -Force

# Copy DLL and dependencies to the RevitLogger subfolder
$filesToCopy = @("RevitLogger.dll", "RevitLogger.pdb", "Newtonsoft.Json.dll")
foreach ($f in $filesToCopy) {
    $src = "$buildDir\$f"
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination "$destSubDir\$f" -Force
        Write-Host "  Copied: $f" -ForegroundColor Cyan
    }
}

Write-Host ""
Write-Host "Deployed to: $addinsDir" -ForegroundColor Green
Write-Host "Now start Revit $RevitVersion, open a project, and look for the 'BIM Personalization' tab."
Write-Host "Logs: $env:LOCALAPPDATA\RevitPersonalization\logs\"
