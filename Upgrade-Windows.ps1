# Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ProjectPath,
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [string]$BackupDir,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Assert-PortAvailable {
    $listener = $null
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if ($listener) {
        throw "Port $Port is in use (PID $($listener.OwningProcess)). Stop the original service completely before applying the patch."
    }
    # Also work when the Windows TCP inventory cannot be queried.
    foreach ($address in @('127.0.0.1', '::1')) {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $connection = $client.ConnectAsync($address, $Port)
            if ($connection.Wait(400) -and $client.Connected) {
                throw "Port $Port is in use. Stop the original service completely before applying the patch."
            }
        }
        catch {
            if ($client.Connected) { throw }
        }
        finally { $client.Dispose() }
    }
}

$project = (Resolve-Path -LiteralPath $ProjectPath -ErrorAction Stop).ProviderPath
if (-not (Get-Item -LiteralPath $project).PSIsContainer) { throw 'ProjectPath must be a directory.' }
$python = Join-Path $project '.venv\Scripts\python.exe'
$start = Join-Path $project 'start.bat'
$upgrader = Join-Path $PSScriptRoot 'upgrade.py'
foreach ($file in @((Join-Path $project 'main.py'), $python, $start, $upgrader)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Required file is missing: $file. Check the actual installation directory."
    }
}

# A running old instance must not see a partially replaced code tree.
Assert-PortAvailable
$backupArgs = @()
if ($BackupDir) { $backupArgs = @('--backup-dir', $BackupDir) }
& $python -B $upgrader check --project $project @backupArgs
if ($LASTEXITCODE -ne 0) { throw 'Original installation or patch checks failed; no patch was applied.' }
& $python -B $upgrader apply --project $project @backupArgs
if ($LASTEXITCODE -ne 0) { throw 'Patch application failed. Read the backup/recovery information above.' }

if ($NoStart) {
    Write-Host "Patch files processed. Start the original service with: `"$start`" --port $Port"
    return
}
# Catch an auto-restart that took the port while the patch was being applied.
Assert-PortAvailable
$originalLocation = Get-Location
$originalPort = $env:AISTUDIO_PORT
try {
    Set-Location -LiteralPath $project -ErrorAction Stop
    $env:AISTUDIO_PORT = [string]$Port
    Write-Host "Starting original service at http://localhost:$Port. Keep this window open."
    & $start --port $Port
    if ($LASTEXITCODE -ne 0) { throw "The original service exited with code $LASTEXITCODE. Read its output above." }
}
finally {
    Set-Location -LiteralPath $originalLocation.Path
    if ($null -eq $originalPort) { Remove-Item Env:\AISTUDIO_PORT -ErrorAction SilentlyContinue }
    else { $env:AISTUDIO_PORT = $originalPort }
}
