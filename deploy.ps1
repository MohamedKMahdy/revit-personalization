# deploy.ps1 — copy the built DLL into the Revit 2027 add-ins folder
# Run this AFTER closing Revit (the DLL is locked while Revit is open)

$src  = "$PSScriptRoot\revit_addin\bin\Release\net8.0-windows\RevitLogger.dll"
$dest = "$env:APPDATA\Autodesk\Revit\Addins\2027\RevitLogger.dll"

if (-not (Test-Path $src)) {
    Write-Error "Build output not found: $src  — run 'dotnet build -c Release' first."
    exit 1
}

Copy-Item -Path $src -Destination $dest -Force
Write-Host "Deployed: $dest" -ForegroundColor Green
Write-Host "Now start Revit 2027, open a project, place some elements, then check:"
Write-Host "  $env:LOCALAPPDATA\RevitPersonalization\logs\"
