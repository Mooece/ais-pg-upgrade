# Requires -Version 5.1
<#
.SYNOPSIS
Run pytest with temporary files, cache and reports outside the workspace.
.EXAMPLE
& .\scripts\test.ps1 -PytestArgs @('-q', '-k', 'analytics')
.EXAMPLE
& .\scripts\test.ps1 -ArtifactsRoot 'E:\test-output' -PythonExe 'E:\venvs\api\Scripts\python.exe'
#>
param(
    [string]$ArtifactsRoot = $env:GPT_TESTS_ROOT,
    [string]$PythonExe,
    [string[]]$PytestArgs = @()
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$forwardArgs = @($PytestArgs) + @($args)
foreach ($argument in $forwardArgs) {
    if ($argument -eq '--' -or $argument -match '^@|^-o|^-c|^--(basetemp|junit|override-ini|config-file|rootdir)') {
        throw "Managed pytest paths/configuration cannot be overridden: $argument"
    }
}
if ([string]::IsNullOrWhiteSpace($ArtifactsRoot)) {
    $ArtifactsRoot = Join-Path (Split-Path -Parent $workspaceRoot) ((Split-Path -Leaf $workspaceRoot) + '-tests')
}
if ($ArtifactsRoot -notmatch '^[A-Za-z]:[\\/]|^\\\\[^\\]+\\[^\\]+') {
    throw 'ArtifactsRoot must be an absolute filesystem directory.'
}
$ArtifactsRoot = [IO.Path]::GetFullPath($ArtifactsRoot)
if ($ArtifactsRoot -eq [IO.Path]::GetPathRoot($ArtifactsRoot) -or $ArtifactsRoot -match '[ .]($|[\\/])') {
    throw 'ArtifactsRoot must name a directory below a drive/share root, without trailing dots or spaces.'
}
$ArtifactsRoot = $ArtifactsRoot.TrimEnd('\', '/')
if ($ArtifactsRoot.Equals($workspaceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $ArtifactsRoot.StartsWith($workspaceRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "ArtifactsRoot must be outside the workspace: $workspaceRoot"
}
# Reject existing junction/symlink ancestors, which could redirect output into the workspace.
for ($ancestor = $ArtifactsRoot; $ancestor; $ancestor = Split-Path -Parent $ancestor) {
    if (Test-Path -LiteralPath $ancestor) {
        $item = Get-Item -LiteralPath $ancestor -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "ArtifactsRoot requires ordinary directories without junctions/symlinks: $ancestor"
        }
    }
}
if (-not $PythonExe) { $PythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$PythonExe = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).ProviderPath
$uv = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [Guid]::NewGuid().ToString('N')
$runDir = Join-Path $ArtifactsRoot (Join-Path 'aistudio-api\runs' $runId)
$tempDir = Join-Path $runDir 'system-tmp'
$cacheDir = Join-Path $runDir 'cache'
New-Item -ItemType Directory -Path $tempDir, $cacheDir -Force | Out-Null
$environment = @{
    PYTHONDONTWRITEBYTECODE = '1'
    PYTEST_ADDOPTS = $null # Use -PytestArgs; project pyproject.toml addopts are preserved.
    TEMP = $tempDir
    TMP = $tempDir
    TMPDIR = $tempDir
    UV_CACHE_DIR = (Join-Path $runDir 'uv-cache')
}
$savedEnvironment = @{}
$originalLocation = Get-Location
$exitCode = 1
try {
    foreach ($name in $environment.Keys) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        if ($null -eq $environment[$name]) { Remove-Item -LiteralPath ('Env:\' + $name) -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process') }
    }
    Set-Location -LiteralPath $projectRoot
    Write-Host "Test artifacts: $runDir"
    # No project sync, dependency installation, Python download or .env loading.
    $uvArgs = @('run', '--no-project', '--offline', '--no-python-downloads',
        '--no-env-file', '--no-config', '--python', $PythonExe, 'python', '-B')
    & $uv @uvArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Selected Python is unavailable or older than 3.11. Supply -PythonExe; this script does not repair environments.' }
    $managedArgs = @('-c', (Join-Path $projectRoot 'pyproject.toml'), '--rootdir', $projectRoot,
        '--basetemp', (Join-Path $runDir 'tmp'), '-o', "cache_dir=$cacheDir",
        '--junitxml', (Join-Path $runDir 'junit.xml'))
    & $uv @uvArgs -m pytest @forwardArgs @managedArgs
    $exitCode = $LASTEXITCODE
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        if ($null -eq $savedEnvironment[$name]) { Remove-Item -LiteralPath ('Env:\' + $name) -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process') }
    }
    Set-Location -LiteralPath $originalLocation.Path
}
exit $exitCode
