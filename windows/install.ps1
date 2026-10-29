[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\miot-panel"),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "miot-panel-data"),
    [int]$Port = 8080,
    [switch]$AtLogon,
    [switch]$Kiosk,
    [switch]$OpenFirewall,
    [switch]$NoShortcut,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Source = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Source "miot-cloud.py"))) {
    throw "miot-cloud.py not found. Run this script from the extracted package/windows folder."
}
if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid port: $Port" }

$py = $null
$pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $py = "py"
    $pyArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = "python"
} else {
    throw "Python 3.9+ was not found. Install it from python.org and enable 'Add Python to PATH'."
}
& $py @pyArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.9+ is required." }

Write-Host "== MIOT Panel for Windows"
Write-Host "   app:  $InstallDir"
Write-Host "   data: $DataDir"
Write-Host "   port: $Port"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "icons") | Out-Null

$files = @(
    "miot-cloud.py", "cloud_panel.html", "admin.html", "camera.html",
    "manifest.webmanifest", "sw.js", "requirements.txt", "README.md", "LICENSE"
)
foreach ($name in $files) {
    $src = Join-Path $Source $name
    if (Test-Path $src) { Copy-Item $src (Join-Path $InstallDir $name) -Force }
}
Copy-Item (Join-Path $Source "icons\*") (Join-Path $InstallDir "icons") -Force
Copy-Item (Join-Path $PSScriptRoot "start-panel.ps1") $InstallDir -Force
Copy-Item (Join-Path $PSScriptRoot "stop-panel.ps1") $InstallDir -Force

$config = Join-Path $DataDir "cloud_config.json"
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $Source "config.example.json") $config -Force
    Write-Host "== created default config: $config"
} else {
    Write-Host "== keeping existing config: $config"
}

$venv = Join-Path $InstallDir ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "== creating Python environment"
    & $py @pyArgs -m venv $venv
}
$venvPy = Join-Path $venv "Scripts\python.exe"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

if ($OpenFirewall) {
    try {
        New-NetFirewallRule -DisplayName "MIOT Panel $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction Stop | Out-Null
        New-NetFirewallRule -DisplayName "MIOT Panel Discovery" -Direction Inbound -Protocol UDP -LocalPort 37020 -Action Allow -ErrorAction SilentlyContinue | Out-Null
        Write-Host "== firewall rule added for TCP $Port and UDP 37020"
    } catch {
        Write-Warning "Could not add firewall rule (run PowerShell as Administrator)."
    }
}

if (-not $NoShortcut) {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $lnk = Join-Path $desktop "MIOT Panel.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = "powershell.exe"
        $args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallDir\start-panel.ps1`" -Port $Port -DataDir `"$DataDir`""
        if ($Kiosk) { $args += " -Kiosk" }
        $sc.Arguments = $args
        $sc.WorkingDirectory = $InstallDir
        $sc.Save()
        Write-Host "== desktop shortcut: $lnk"
    } catch {
        Write-Warning "Could not create desktop shortcut: $_"
    }
}

if ($AtLogon) {
    try {
        $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallDir\start-panel.ps1`" -Port $Port -DataDir `"$DataDir`""
        if ($Kiosk) { $arg += " -Kiosk" }
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName "MIOT Panel" -Action $action -Trigger $trigger -Description "MIOT cloud wall panel" -RunLevel Limited -Force | Out-Null
        Write-Host "== scheduled task 'MIOT Panel' created (runs at logon)"
    } catch {
        Write-Warning "Could not create scheduled task: $_"
    }
}

if (-not $NoStart) {
    & (Join-Path $InstallDir "start-panel.ps1") -Port $Port -DataDir $DataDir -Kiosk:$Kiosk
}
Write-Host ""
Write-Host "Panel: http://127.0.0.1:$Port/"
Write-Host "Admin: http://127.0.0.1:$Port/admin"
Write-Host "Start again: powershell -ExecutionPolicy Bypass -File `"$InstallDir\start-panel.ps1`" -Port $Port"
