$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$destination = Join-Path $project '.eval-tools/promptfoo'
$cache = Join-Path $project '.eval-tools/npm-cache'
New-Item -ItemType Directory -Force -Path $destination, $cache | Out-Null
foreach ($name in @('package.json', 'package-lock.json')) {
    $source = Join-Path $PSScriptRoot $name
    $target = Join-Path $destination $name
    if ((Test-Path -LiteralPath $target) -and
        ((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash)) {
        throw "Existing isolated installation has a different $name; choose a separate installation before replacing it."
    }
    Copy-Item -LiteralPath $source -Destination $target
}
& npm.cmd ci --prefix $destination --cache $cache --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "Pinned promptfoo installation failed: $LASTEXITCODE" }
& npm.cmd ls --prefix $destination --depth=0
if ($LASTEXITCODE -ne 0) { throw 'Installed dependency tree is invalid' }
