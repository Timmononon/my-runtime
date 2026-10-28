[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$PythonExe = '',
    [string]$VenvPath = '',
    [string]$IndexUrl = 'https://pypi.org/simple',
    [switch]$InstallBrowser,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrEmpty($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
if ([string]::IsNullOrEmpty($PythonExe)) {
    $PythonExe = 'python'
}
$projectPath = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($VenvPath)) {
    $VenvPath = Join-Path $projectPath '.venv'
}
$venvAbsolute = [System.IO.Path]::GetFullPath($VenvPath)
$runtimePython = Join-Path $venvAbsolute 'Scripts\python.exe'
$lockFile = Join-Path $projectPath 'requirements.lock'
if (-not (Test-Path -LiteralPath $lockFile -PathType Leaf)) {
    throw "Missing dependency lock: $lockFile"
}
if ($DryRun) {
    [pscustomobject]@{
        Project = $projectPath; VirtualEnvironment = $venvAbsolute;
        Requirements = $lockFile; IndexUrl = $IndexUrl; InstallBrowser = [bool]$InstallBrowser;
        GlobalEnvironmentChanged = $false; AutostartEnabled = $false
    } | ConvertTo-Json
    return
}
function Invoke-AsterNative {
    param([string]$Executable, [string[]]$NativeArguments)
    & $Executable @NativeArguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable" }
}
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    Invoke-AsterNative -Executable $PythonExe -NativeArguments @('-c', 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"')
    Invoke-AsterNative -Executable $PythonExe -NativeArguments @('-m', 'venv', $venvAbsolute)
}
Invoke-AsterNative -Executable $runtimePython -NativeArguments @('-m', 'pip', 'install', '--index-url', $IndexUrl, '-r', $lockFile)
Invoke-AsterNative -Executable $runtimePython -NativeArguments @('-m', 'pip', 'install', '--index-url', $IndexUrl, '--no-deps', '-e', $projectPath)
if ($InstallBrowser) {
    Invoke-AsterNative -Executable $runtimePython -NativeArguments @('-m', 'playwright', 'install', 'chromium')
}
Write-Output "Installed Aster in $venvAbsolute. No autostart entry was created."
