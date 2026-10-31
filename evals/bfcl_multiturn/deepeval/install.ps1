param([string]$PythonExecutable = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$venvPath = Join-Path $projectRoot '.eval-tools\deepeval-venv'
$isolatedPython = Join-Path $venvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $isolatedPython)) {
    & $PythonExecutable -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create isolated DeepEval environment' }
}
& $isolatedPython -m pip install --disable-pip-version-check --cache-dir (Join-Path $projectRoot '.eval-tools\pip-cache') -r (Join-Path $PSScriptRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pinned DeepEval dependency installation failed' }
& $isolatedPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'DeepEval dependency compatibility check failed' }
