# deploy.ps1 — copy the built DLL into the Revit add-ins folder.
# Run AFTER closing Revit (the DLL is locked while Revit is open).
#
# Usage:
#   .\deploy.ps1                    # default: Revit 2027 (net10.0-windows)
#   .\deploy.ps1 -RevitVersion 2026 # Revit 2026 (net8.0-windows)

param([string]$RevitVersion = "2027")

# Revit 2027 hosts .NET 10; Revit 2025/2026 host .NET 8.
$tfm = if ($RevitVersion -eq "2027") { "net10.0-windows" } else { "net8.0-windows" }

$src  = "$PSScriptRoot\revit_addin\bin\Release\$tfm\RevitLogger.dll"
$dest = "$env:APPDATA\Autodesk\Revit\Addins\$RevitVersion\RevitLogger.dll"

if (-not (Test-Path $src)) {
    Write-Error "Build output not found: $src  — run 'dotnet build revit_addin\RevitLogger.csproj -c Release -p:RevitVersion=$RevitVersion' first."
    exit 1
}

Copy-Item -Path $src -Destination $dest -Force
Write-Host "Deployed: $dest" -ForegroundColor Green
Write-Host "Now start Revit $RevitVersion, open a project, place some elements, then check:"
Write-Host "  $env:LOCALAPPDATA\RevitPersonalization\logs\"
