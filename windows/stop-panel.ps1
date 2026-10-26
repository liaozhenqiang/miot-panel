[CmdletBinding()]
param([int]$Port = 8080)
$ErrorActionPreference = "Stop"
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Write-Host "No listener on port $Port."
    exit 0
}
$procId = $conn.OwningProcess
$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "Stopping $($proc.ProcessName) (PID $procId) on port $Port"
    Stop-Process -Id $procId -Force
} else {
    Write-Host "Listener PID $procId is no longer running."
}
