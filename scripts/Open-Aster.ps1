[CmdletBinding()]
param(
    [string]$DataDir = '',
    [string]$PythonExe = '',
    [string]$Cwd = (Get-Location).Path,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Aster-Paths.ps1')
$DataDir = Resolve-AsterDataDirectory -DataDir $DataDir -Explicit:($PSBoundParameters.ContainsKey('DataDir'))
if ([string]::IsNullOrEmpty($PythonExe)) {
    $PythonExe = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Missing runtime Python: $PythonExe" }
if (-not (Test-Path -LiteralPath $Cwd -PathType Container)) { throw "Missing workspace directory: $Cwd" }
# Reuse the CLI's owned-daemon identity checks and one-time browser login.
# The CLI prints only the base URL; login tickets and credentials stay out of logs.
$asterArguments = @('-X', 'utf8', '-m', 'aster', '--data-dir', $DataDir,
    'web', '--cwd', (Resolve-Path -LiteralPath $Cwd).Path, '--port', [string]$Port)
if ($NoBrowser) { $asterArguments += '--no-browser' }
$previousConsoleEncoding = [Console]::OutputEncoding
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & $PythonExe @asterArguments
    if ($LASTEXITCODE -ne 0) { throw "Aster workbench failed to open ($LASTEXITCODE)" }
} finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
}
