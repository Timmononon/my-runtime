[CmdletBinding()]
param(
    [string]$DataDir = '',
    [ValidateSet('1.0.100')]
    [string]$Version = '1.0.100',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:ASTER_DATA_DIR)) {
        $DataDir = $env:ASTER_DATA_DIR
    } elseif (-not [string]::IsNullOrWhiteSpace($env:ASTER_HOME)) {
        $DataDir = $env:ASTER_HOME
    } else {
        $DataDir = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.aster'
    }
}
$dataPath = [System.IO.Path]::GetFullPath($DataDir)
$toolRoot = [System.IO.Path]::GetFullPath((Join-Path $dataPath 'tools\feishu-cli'))
$packageRoot = Join-Path $toolRoot 'npm-package'
$nativePath = Join-Path $toolRoot 'lark-cli.exe'
$authConfigDirectory = Join-Path $dataPath 'feishu-cli'
$packageBinary = Join-Path $packageRoot 'node_modules\@larksuite\cli\bin\lark-cli.exe'
$packageManifest = Join-Path $packageRoot 'node_modules\@larksuite\cli\package.json'
$packageInstaller = Join-Path $packageRoot 'node_modules\@larksuite\cli\scripts\install.js'
$lockPath = Join-Path $packageRoot 'package-lock.json'

if ($DryRun) {
    [pscustomobject]@{
        DataDirectory = $dataPath; ToolDirectory = $toolRoot; Executable = $nativePath;
        Package = '@larksuite/cli'; Version = $Version; Registry = 'https://registry.npmjs.org';
        PackageDirectory = $packageRoot; AuthConfigDirectory = $authConfigDirectory;
        AuthorizesAccount = $false; ChangesPath = $false
    } | ConvertTo-Json
    return
}
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This installer supports Windows. Install the official native CLI manually on other platforms.'
}
$npmCommand = Get-Command 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$nodeCommand = Get-Command 'node.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $npmCommand -or -not $nodeCommand) {
    throw 'Node.js with npm is required to install the official Feishu CLI.'
}

function Assert-FeishuToolPath {
    param([string]$Path)
    $absolute = [System.IO.Path]::GetFullPath($Path)
    $prefix = $toolRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $absolute.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The installation path escapes the selected Feishu tool directory.'
    }
}

Assert-FeishuToolPath -Path $packageBinary
Assert-FeishuToolPath -Path $nativePath
[void][System.IO.Directory]::CreateDirectory($packageRoot)
# Download without running dependency lifecycle scripts. Invoke only the known
# official native installer after checking the package; npm 11 may otherwise
# skip it under allow-scripts. Do not change the global npm approval policy.
& $npmCommand.Source '--prefix' $packageRoot 'install' '--ignore-scripts' '--no-audit' '--no-fund' '--save-exact' `
    "@larksuite/cli@$Version" '--registry=https://registry.npmjs.org'
if ($LASTEXITCODE -ne 0) { throw "Official Feishu CLI installation failed ($LASTEXITCODE)." }
if (-not (Test-Path -LiteralPath $packageInstaller -PathType Leaf) -or
    -not (Test-Path -LiteralPath $packageManifest -PathType Leaf) -or
    -not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw 'The official package did not supply its native binary and dependency lock.'
}
$installedPackage = Get-Content -LiteralPath $packageManifest -Raw | ConvertFrom-Json
if ($installedPackage.name -ne '@larksuite/cli' -or $installedPackage.version -ne $Version) {
    throw 'The installed Feishu package differs from the requested package and version.'
}
& $nodeCommand.Source $packageInstaller
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $packageBinary -PathType Leaf)) {
    throw 'The official checksum-verified native Feishu executable installation failed.'
}
$versionOutput = & $packageBinary '--version' 2>&1
if ($LASTEXITCODE -ne 0 -or ($versionOutput -join "`n") -notmatch
    ('(?<![0-9])' + [regex]::Escape($Version) + '(?![0-9])')) {
    throw 'The downloaded Feishu executable did not report the requested version.'
}

$staging = Join-Path $toolRoot ('lark-cli-' + [Guid]::NewGuid().ToString('N') + '.staging.exe')
Assert-FeishuToolPath -Path $staging
try {
    [System.IO.File]::Copy($packageBinary, $staging, $false)
    $sourceHash = (Get-FileHash -LiteralPath $packageBinary -Algorithm SHA256).Hash
    if ((Get-FileHash -LiteralPath $staging -Algorithm SHA256).Hash -ne $sourceHash) {
        throw 'Feishu executable verification failed before activation.'
    }
    # Both paths are verified children of the selected tool root. Existing
    # executables stay in place if atomic replacement cannot complete.
    if (Test-Path -LiteralPath $nativePath -PathType Leaf) {
        [System.IO.File]::Replace($staging, $nativePath, [NullString]::Value)
    } else {
        [System.IO.File]::Move($staging, $nativePath)
    }
    [pscustomobject]@{
        DataDirectory = $dataPath; Executable = $nativePath; Version = $Version;
        SHA256 = $sourceHash.ToLowerInvariant(); PackageLock = $lockPath; AuthConfigDirectory = $authConfigDirectory;
        AccountAuthorization = 'not_started'; ChangesPath = $false
    } | ConvertTo-Json
} finally {
    if (Test-Path -LiteralPath $staging -PathType Leaf) {
        Assert-FeishuToolPath -Path $staging
        Remove-Item -LiteralPath $staging
    }
}
