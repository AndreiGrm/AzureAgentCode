$ErrorActionPreference = "Stop"

$workflowPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$dashboardUrl = "http://127.0.0.1:8765"
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1

if (-not $listener) {
    Start-Process -FilePath "python" -ArgumentList "dashboard_server.py" -WorkingDirectory $workflowPath
    for ($attempt = 0; $attempt -lt 15; $attempt += 1) {
        Start-Sleep -Milliseconds 500
        $listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($listener) {
            break
        }
    }
}

if (-not $listener) {
    throw "La dashboard non si e' avviata sulla porta 8765."
}

Start-Process $dashboardUrl
