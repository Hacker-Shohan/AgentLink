# AI Agent PC Harness — Windows bootstrap
# Fetched and run via the -EncodedCommand one-liner the dashboard generates
# (see server/auth.py: build_windows_bootstrap_command). Not meant to be
# pasted directly — the {{...}} placeholders below are substituted server-side.
#
# Uses curl.exe (bundled with Windows 10 1803+ / Windows 11) for every HTTPS
# fetch instead of Invoke-WebRequest. iwr goes through .NET's own TLS stack,
# which on Windows PowerShell 5.1 can fail the handshake against a modern
# ECDSA self-signed cert ("underlying connection was closed") regardless of
# any ServerCertificateValidationCallback override — the handshake itself
# never completes, so there's nothing for the callback to approve. curl.exe
# carries its own TLS implementation and doesn't hit this.
param()

$ErrorActionPreference = "Stop"

$ServerBase = "{{SERVER_BASE}}"
$Token      = "{{TOKEN}}"
$DeviceId   = "{{DEVICE_ID}}"

if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    Write-Error "curl.exe not found (ships with Windows 10 1803+/Windows 11). Update Windows or install curl manually."
    exit 1
}

$installDir = "$env:LOCALAPPDATA\agent-harness"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3 is required. Install it from https://python.org and re-run this command."
    exit 1
}

python -m pip install --quiet --user websocket-client

$clientPath = "$installDir\harness_client.py"
curl.exe -ksSL "$ServerBase/client/harness_client.py" -o $clientPath
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $clientPath)) {
    Write-Error "Failed to download harness_client.py from $ServerBase"
    exit 1
}

$wsServer = $ServerBase -replace '^https://', 'wss://' -replace '^http://', 'ws://'

Start-Process -WindowStyle Hidden python -ArgumentList @(
    "`"$clientPath`"",
    "--server", $wsServer,
    "--token", $Token,
    "--insecure",
    "--reset"
)

Write-Host "Harness client started. Device ID: $DeviceId"
