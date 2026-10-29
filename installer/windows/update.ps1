# Update a FlightRisk install made by installer\windows\install.ps1.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File update.ps1             newest release
#   powershell -NoProfile -ExecutionPolicy Bypass -File update.ps1 -Ref v1.2.3 that release (or main)
#   powershell -NoProfile -ExecutionPolicy Bypass -File update.ps1 -Check      print one JSON line; change nothing
#
# The update is the installer run again with FLIGHTRISK_REF set, so it follows the
# installer's rules: it refuses a folder the installer did not make, local changes
# and a running FlightRisk (it never stops FlightRisk itself, because an edge may be
# flying a drone), and it never moves an install backwards. If installing the
# packages of the new release fails, the folder is put back on the release it had.
#
# The installer is copied out of the install folder and run from the copy, so the
# git checkout that replaces it cannot affect the run.
param(
    [string]$Ref = "",
    [switch]$Check
)

$ErrorActionPreference = "Stop"

# A GIT_DIR or GIT_WORK_TREE (or any other GIT_* setting) in the caller's
# environment would point every git command at another repository.
Get-ChildItem Env: | Where-Object { $_.Name -like "GIT_*" } | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }

$FlightRiskHome = if ($env:FLIGHTRISK_HOME) { $env:FLIGHTRISK_HOME } else { Join-Path $env:LOCALAPPDATA "FlightRisk" }
$AppDir = Join-Path $FlightRiskHome "app"
$Marker = Join-Path (Join-Path $AppDir ".git") "flightrisk-installer"
$Installer = Join-Path (Join-Path (Join-Path $AppDir "installer") "windows") "install.ps1"

if (-not (Test-Path -LiteralPath $Marker)) {
    Write-Host "update.ps1: $AppDir is not a FlightRisk install made by the installer; nothing was changed." -ForegroundColor Red
    exit 2
}

# The installer's own functions (Select-LatestTag, Test-RefName, Get-MarkerRef).
$env:FLIGHTRISK_INSTALL_LIB_ONLY = "1"
try {
    . $Installer
} finally {
    Remove-Item Env:FLIGHTRISK_INSTALL_LIB_ONLY -ErrorAction SilentlyContinue
}
$script:AppDir = $AppDir

$ErrorActionPreference = "Continue"
$repoUrl = if ($env:FLIGHTRISK_REPO_URL) { $env:FLIGHTRISK_REPO_URL } else { (& git -C $AppDir remote get-url origin).Trim() }
$tagLines = @(& git ls-remote --tags --refs $repoUrl 2>$null)
if ($LASTEXITCODE -ne 0) {
    Write-Host "update.ps1: could not reach $repoUrl to look for releases; nothing was changed." -ForegroundColor Red
    exit 1
}
$latest = Select-LatestTag $tagLines
$installed = Get-MarkerRef

if ($Check) {
    $available = $false
    if ($latest -and $latest -ne $installed) {
        if ($installed -eq "main") {
            $available = $true
        } else {
            & git -C $AppDir merge-base --is-ancestor $latest HEAD 2>$null
            $available = ($LASTEXITCODE -ne 0)
        }
    }
    [ordered]@{ installDir = $AppDir; installed = $installed; latest = $latest; updateAvailable = $available } | ConvertTo-Json -Compress
    exit 0
}

$target = if ($Ref) { $Ref } elseif ($latest) { $latest } else { "main" }
if (-not (Test-RefName $target)) {
    Write-Host "update.ps1: -Ref must be a release tag like v1.2.3, or main (got '$target')." -ForegroundColor Red
    exit 64
}
$copy = Join-Path ([System.IO.Path]::GetTempPath()) "flightrisk-install-$PID.ps1"
Copy-Item -LiteralPath $Installer -Destination $copy -Force
try {
    $env:FLIGHTRISK_REF = $target
    $env:FLIGHTRISK_HOME = $FlightRiskHome
    $env:FLIGHTRISK_REPO_URL = $repoUrl
    $shell = (Get-Process -Id $PID).Path
    & $shell -NoProfile -ExecutionPolicy Bypass -File $copy
    exit $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $copy -Force -ErrorAction SilentlyContinue
}
