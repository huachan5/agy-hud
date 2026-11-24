$ErrorActionPreference = "Stop"

$InstallDir = if ($env:AGY_HUD_DIR) { $env:AGY_HUD_DIR } else { Join-Path $env:USERPROFILE ".antigravity\agy-hud" }
$SettingsFile = if ($env:AGY_SETTINGS_FILE) { $env:AGY_SETTINGS_FILE } else { Join-Path $env:USERPROFILE ".gemini\antigravity-cli\settings.json" }
$BackupDir = Join-Path $InstallDir "backups"

New-Item -ItemType Directory -Force -Path $InstallDir, $BackupDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

if (Test-Path $SettingsFile) {
    Copy-Item $SettingsFile (Join-Path $BackupDir "settings.json.$Timestamp.bak") -Force
}
if (Test-Path (Join-Path $InstallDir "hud.py")) {
    Copy-Item (Join-Path $InstallDir "hud.py") (Join-Path $BackupDir "hud.py.$Timestamp.bak") -Force
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Copy-Item (Join-Path $ScriptDir "hud.py") (Join-Path $InstallDir "hud.py") -Force

$TargetConfig = Join-Path $InstallDir "config.json"
if (-not (Test-Path $TargetConfig)) {
    Copy-Item (Join-Path $ScriptDir "config.json") $TargetConfig -Force
}

# Resolve real Python executable (skip WindowsApps Store stub)
$PythonExe = (Get-Command python -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*\WindowsApps\*" } | Select-Object -First 1 -ExpandProperty Source)
if (-not $PythonExe) {
    $PythonExe = "python"
}

$HudPyPath = Join-Path $InstallDir "hud.py"
$SettingsDir = Split-Path -Parent $SettingsFile
New-Item -ItemType Directory -Force -Path $SettingsDir | Out-Null

$PyScript = @'
import json, os, sys
settings_path, python_exe, hud_path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)
except FileNotFoundError:
    settings = {}

def quote_if_needed(p: str) -> str:
    return f'"{p}"' if " " in p else p

settings["statusLine"] = {
    "type": "command",
    "command": f"{quote_if_needed(python_exe)} {quote_if_needed(hud_path)}",
    "enabled": True,
    "padding": 0,
}
tmp_path = settings_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp_path, settings_path)
'@

$PyScript | & $PythonExe - $SettingsFile $PythonExe $HudPyPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update $SettingsFile (Python exited with code $LASTEXITCODE)"
}

Write-Host "agy-hud installed successfully!" -ForegroundColor Green
Write-Host "  Script  : $HudPyPath"
Write-Host "  Config  : $TargetConfig"
Write-Host "  Settings: $SettingsFile"
Write-Host "  Backups : $BackupDir"
Write-Host "Restart Antigravity CLI (agy) to activate the new HUD."
