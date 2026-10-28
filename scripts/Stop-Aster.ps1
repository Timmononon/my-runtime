[CmdletBinding()]
param(
    [string]$DataDir = '',
    [string]$PythonExe = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Aster-Paths.ps1')
$DataDir = Resolve-AsterDataDirectory -DataDir $DataDir -Explicit:($PSBoundParameters.ContainsKey('DataDir'))
if ([string]::IsNullOrEmpty($PythonExe)) {
    $PythonExe = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
}
& $PythonExe -m aster --data-dir $DataDir daemon stop
if ($LASTEXITCODE -ne 0) { throw "Aster failed to stop ($LASTEXITCODE)" }
