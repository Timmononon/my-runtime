function Resolve-AsterDataDirectory {
    param([string]$DataDir, [switch]$Explicit)
    if (-not $Explicit) {
        if (-not [string]::IsNullOrEmpty($env:ASTER_DATA_DIR)) {
            $DataDir = $env:ASTER_DATA_DIR
        } elseif (-not [string]::IsNullOrEmpty($env:ASTER_HOME)) {
            $DataDir = $env:ASTER_HOME
        } else {
            $DataDir = Join-Path $env:USERPROFILE '.aster'
        }
    }
    if ([string]::IsNullOrEmpty($DataDir)) {
        $DataDir = (Get-Location).Path
    } elseif ($DataDir -eq '~') {
        $DataDir = $env:USERPROFILE
    } elseif ($DataDir.StartsWith('~\') -or $DataDir.StartsWith('~/')) {
        $DataDir = Join-Path $env:USERPROFILE $DataDir.Substring(2)
    }
    if (-not [System.IO.Path]::IsPathRooted($DataDir)) {
        $DataDir = Join-Path (Get-Location).Path $DataDir
    }
    $resolved = [System.IO.Path]::GetFullPath($DataDir)
    # Match pathlib's resolved directory form and avoid Windows PowerShell 5.1
    # quoting a space-containing native argument with a trailing backslash.
    if ($resolved.Length -gt [System.IO.Path]::GetPathRoot($resolved).Length) {
        $resolved = $resolved.TrimEnd([char[]]'\/')
    }
    return $resolved
}
