[CmdletBinding()]
param(
    [string]$DataDir = '',
    [string]$PythonExe = '',
    [string]$Cwd = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Aster-Paths.ps1')

function Quote-AsterShortcutArgument([string]$Value) {
    # Double trailing backslashes before the Windows command-line closing quote.
    return '"' + ($Value -replace '(\\+)$', '$1$1') + '"'
}

$asterRoot = Split-Path -Parent $PSScriptRoot
$desktopApp = Join-Path $asterRoot 'desktop'
$DataDir = Resolve-AsterDataDirectory -DataDir $DataDir -Explicit:($PSBoundParameters.ContainsKey('DataDir'))
if (-not $PythonExe) { $PythonExe = Join-Path $asterRoot '.venv\Scripts\python.exe' }
if (-not $Cwd) { $Cwd = $asterRoot }
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Missing runtime Python: $PythonExe" }
if (-not (Test-Path -LiteralPath $Cwd -PathType Container)) { throw "Missing workspace directory: $Cwd" }
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$Cwd = (Resolve-Path -LiteralPath $Cwd).Path

$electronModule = Join-Path $desktopApp 'node_modules\electron'
if (-not (Test-Path -LiteralPath (Join-Path $electronModule 'package.json') -PathType Leaf)) {
    & npm.cmd --prefix $desktopApp ci --include=dev --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Electron dependencies ($LASTEXITCODE)" }
}
$electronExe = & node -e 'console.log(require(process.argv[1]))' $electronModule | Select-Object -Last 1
if ($LASTEXITCODE -ne 0 -or -not $electronExe -or -not (Test-Path -LiteralPath $electronExe -PathType Leaf)) {
    throw 'Unable to locate the Electron executable'
}

$shortcutArguments = @(
    (Quote-AsterShortcutArgument $desktopApp)
    (Quote-AsterShortcutArgument "--aster-python=$PythonExe")
    (Quote-AsterShortcutArgument "--aster-data-dir=$DataDir")
    (Quote-AsterShortcutArgument "--aster-cwd=$Cwd")
    "--aster-port=$Port"
)
$desktopFolder = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopFolder 'Aster.lnk'
$previousShortcut = Join-Path $desktopFolder 'Aster 桌面版.lnk'
$shellLinks = New-Object -ComObject WScript.Shell
$shortcut = $shellLinks.CreateShortcut($shortcutPath)
if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.TargetPath -ne $electronExe) {
    $legacyScript = [regex]::Escape((Join-Path $PSScriptRoot 'Open-Aster.ps1'))
    $isBrowserShortcut = [IO.Path]::GetFileName($shortcut.TargetPath) -in @('powershell.exe', 'pwsh.exe') -and
        $shortcut.Arguments -match ('(?i)(?:^|\s)-File\s+(?:"' + $legacyScript + '"|' + $legacyScript + ')(?:\s|$)')
    if (-not $isBrowserShortcut) {
        $shortcutPath = $previousShortcut
        $shortcut = $shellLinks.CreateShortcut($shortcutPath)
        if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.TargetPath -ne $electronExe) {
            throw 'Both Aster shortcut names are used by other applications.'
        }
    }
}
$shortcut.TargetPath = $electronExe
$shortcut.Arguments = $shortcutArguments -join ' '
$shortcut.WorkingDirectory = $asterRoot
$shortcut.Description = 'Open the Aster desktop workbench'
$icon = Join-Path $desktopApp 'assets\aster.ico'
if (Test-Path -LiteralPath $icon -PathType Leaf) { $shortcut.IconLocation = "$icon,0" }
$shortcut.Save()
if ($shortcutPath -ne $previousShortcut -and (Test-Path -LiteralPath $previousShortcut -PathType Leaf)) {
    $previous = $shellLinks.CreateShortcut($previousShortcut)
    if ($previous.TargetPath -eq $electronExe -and $previous.Arguments -eq $shortcut.Arguments) {
        Remove-Item -LiteralPath $previousShortcut
    }
}
Write-Output $shortcutPath
