# deploy.ps1 — copy the built DLL into the Revit add-ins folder.
# Run AFTER closing Revit (the DLL is locked while Revit is open).
#
# Supported: Revit 2025 and 2026 (both .NET 8 / net8.0-windows).
# Usage:
#   .\deploy.ps1                    # default: Revit 2026
#   .\deploy.ps1 -RevitVersion 2025 # Revit 2025

param([string]$RevitVersion = "2026")

$src  = "$PSScriptRoot\revit_addin\bin\Release\net8.0-windows\RevitLogger.dll"
$dest = "$env:APPDATA\Autodesk\Revit\Addins\$RevitVersion\RevitLogger.dll"

if (-not (Test-Path $src)) {
    Write-Error "Build output not found: $src  — run 'dotnet build revit_addin\RevitLogger.csproj -c Release -p:RevitVersion=$RevitVersion' first."
    exit 1
}

Copy-Item -Path $src -Destination $dest -Force
Write-Host "Deployed: $dest" -ForegroundColor Green
Write-Host "Now start Revit $RevitVersion, open a project, place some elements, then check:"
Write-Host "  $env:LOCALAPPDATA\RevitPersonalization\logs\"
