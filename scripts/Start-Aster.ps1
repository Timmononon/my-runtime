[CmdletBinding()]
param(
    [string]$DataDir = '',
    [string]$PythonExe = '',
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Aster-Paths.ps1')
$DataDir = Resolve-AsterDataDirectory -DataDir $DataDir -Explicit:($PSBoundParameters.ContainsKey('DataDir'))
if ([string]::IsNullOrEmpty($PythonExe)) {
    $PythonExe = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Missing runtime Python: $PythonExe" }
# The CLI helper launches the owned daemon with CREATE_NO_WINDOW and verifies
# its unique instance_id. No raw shell-string process launch is needed here.
& $PythonExe -m aster --data-dir $DataDir daemon start --host '127.0.0.1' --port $Port
if ($LASTEXITCODE -ne 0) { throw "Aster failed to start ($LASTEXITCODE)" }
