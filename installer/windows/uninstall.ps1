# Uninstall FlightRisk from this computer (an install made by installer\windows\install.ps1).
# Settings > Apps > Installed apps > FlightRisk > Uninstall runs this script.
#
#   uninstall.ps1               ask, then uninstall
#   uninstall.ps1 -Yes          uninstall without asking
#   uninstall.ps1 -Yes -Force   ...also when the folder holds changes that are not on GitHub
#   uninstall.ps1 -Check        print one JSON line saying what it would do; change nothing
#   uninstall.ps1 -NoKeepData   do not keep a copy of logs, captures and models (see below)
#
# What it removes: the install folder (%LOCALAPPDATA%\FlightRisk\app, with its
# virtual environment), the status file and lock next to it, the Start menu entry
# (only when it points into this install) and the Installed apps entry (only when
# it names this install).
#
# What it keeps: %USERPROFILE%\.flightrisk (launcher presets, the saved ceiling),
# %USERPROFILE%\.flightrisk-fleet.env (the edge token), Python and git. Before the
# folder is deleted, what FlightRisk wrote into it (logs, captures, recordings,
# models, flightrisk_sessions.db, target_reference.*) is copied to
# Documents\FlightRisk data <date>, unless -NoKeepData is given. Captures can show
# people, so delete that copy when you no longer need it.
#
# What it refuses (and then deletes nothing), with the same exit codes as the macOS script:
#   2  the folder is a link, is not a FlightRisk checkout, or has no
#      .git\flightrisk-installer marker (someone's own checkout is never deleted,
#      not even with -Force)
#   3  FlightRisk is running from the folder (it never stops FlightRisk, because an
#      edge may be flying a drone; quit it from the launcher window)
#   4  the folder holds changes that are not on GitHub, and -Force was not given
#   1  cancelled at the question
param(
    [switch]$Yes,
    [switch]$Force,
    [switch]$Check,
    [switch]$NoKeepData
)

$ErrorActionPreference = "Stop"

$FlightRiskHome = if ($env:FLIGHTRISK_HOME) { $env:FLIGHTRISK_HOME } else { Join-Path $env:LOCALAPPDATA "FlightRisk" }
$AppDir = Join-Path $FlightRiskHome "app"
$VenvDir = Join-Path $AppDir "venv"
$Marker = Join-Path (Join-Path $AppDir ".git") "flightrisk-installer"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlightRisk"
$OnWindows = ($IsWindows -or $env:OS -eq "Windows_NT")
$KeepNames = @("logs", "captures", "recordings", "models", "flightrisk_sessions.db", "flightrisk_sessions.db-wal", "flightrisk_sessions.db-shm")

function Test-IsLink([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

function Test-IsCheckout {
    return (Test-Path -LiteralPath (Join-Path $AppDir ".git")) -and (Test-Path -LiteralPath (Join-Path (Join-Path $AppDir "flightrisk") "__init__.py"))
}

function Get-RunningPids {
    $ErrorActionPreference = "Continue"
    $venv = $VenvDir.TrimEnd('\', '/')
    if ($OnWindows) {
        $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$venv\", [System.StringComparison]::OrdinalIgnoreCase) }
        return @($procs | ForEach-Object { [int]$_.ProcessId })
    }
    $found = @()
    foreach ($line in (& ps -axww -o "pid=,args=" 2>$null)) {
        if ($line -match '^\s*(\d+)\s+(.*)$' -and [int]$Matches[1] -ne $PID -and $Matches[2].StartsWith("$venv/")) { $found += [int]$Matches[1] }
    }
    return $found
}

function Get-UnsavedWork {
    $ErrorActionPreference = "Continue"
    $found = @()
    $changes = @(& git -C $AppDir status --porcelain --untracked-files=no 2>$null)
    if ($changes.Count -gt 0) { $found += "changed files: $($changes.Count)" }
    $commits = @(& git -C $AppDir log HEAD --branches --not --remotes --tags --oneline 2>$null)
    if ($commits.Count -gt 0) { $found += "commits that are not pushed: $($commits.Count)" }
    $stashes = @(& git -C $AppDir stash list 2>$null)
    if ($stashes.Count -gt 0) { $found += "stashes: $($stashes.Count)" }
    return $found
}

function Get-KeptItems {
    $items = @()
    foreach ($name in $KeepNames) {
        if (Test-Path -LiteralPath (Join-Path $AppDir $name)) { $items += $name }
    }
    foreach ($file in (Get-ChildItem -LiteralPath $AppDir -Filter "target_reference.*" -Force -ErrorAction SilentlyContinue)) { $items += $file.Name }
    return $items
}

function Get-StartMenuShortcut {
    if (-not $OnWindows) { return $null }
    return (Join-Path ([Environment]::GetFolderPath("Programs")) "FlightRisk.lnk")
}

# Whether the Start menu entry opens something inside this install.
function Test-OurShortcut {
    $path = Get-StartMenuShortcut
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { return $false }
    $target = (New-Object -ComObject WScript.Shell).CreateShortcut($path).TargetPath
    return [bool]($target -and $target.StartsWith("$AppDir\", [System.StringComparison]::OrdinalIgnoreCase))
}

function Test-OurRegistration {
    if (-not $OnWindows -or -not (Test-Path $UninstallKey)) { return $false }
    return ((Get-ItemProperty -Path $UninstallKey -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation -eq $AppDir)
}

function Write-Refusal([string]$Reason, [string]$Message, [int]$Code) {
    [ordered]@{ removed = $false; reason = $Reason; installDir = $AppDir } | ConvertTo-Json -Compress
    Write-Host "uninstall.ps1: $Message" -ForegroundColor Red
    exit $Code
}

$exists = Test-Path -LiteralPath $AppDir
$isLink = Test-IsLink $AppDir
$unsaved = @()
$keep = @()
if ($exists -and -not $isLink) {
    $unsaved = @(Get-UnsavedWork)
    $keep = @(Get-KeptItems)
}
$running = @(Get-RunningPids)

if ($Check) {
    [ordered]@{
        check              = $true
        installDir         = $AppDir
        exists             = $exists
        isLink             = $isLink
        flightriskCheckout = ($exists -and (Test-IsCheckout))
        installerMade      = (Test-Path -LiteralPath $Marker)
        running            = $running
        unsaved            = $unsaved
        keep               = $keep
        shortcutIsOurs     = (Test-OurShortcut)
        registered         = (Test-OurRegistration)
    } | ConvertTo-Json -Compress
    exit 0
}

# Out of the folder about to be deleted, so this process does not hold it open.
Set-Location -LiteralPath ([System.IO.Path]::GetTempPath())

$alreadyGone = $false
if ($isLink) {
    Write-Refusal "is-a-link" "$AppDir is a link to another folder; nothing was deleted." 2
} elseif (-not $exists) {
    $alreadyGone = $true
} elseif (-not (Test-IsCheckout)) {
    Write-Refusal "not-a-flightrisk-install" "$AppDir does not look like a FlightRisk install; nothing was deleted." 2
} elseif (-not (Test-Path -LiteralPath $Marker)) {
    Write-Refusal "not-made-by-installer" "$AppDir was not made by the FlightRisk installer, so it was not deleted. Delete it yourself if you are sure." 2
}
if ($running.Count -gt 0) {
    Write-Refusal "running" "FlightRisk is running from $AppDir (process $($running -join ', ')). Land the drones, stop everything in the launcher window and quit it, then run this again." 3
}
if ($unsaved.Count -gt 0 -and -not $Force -and $Yes) {
    Write-Refusal "unsaved-work" "$AppDir holds work that is not on GitHub ($($unsaved -join '; ')). Run again with -Force to delete it anyway." 4
}

if (-not $Yes) {
    Write-Host "This removes FlightRisk from this computer:"
    if (-not $alreadyGone) { Write-Host "  $AppDir (with its Python packages)" }
    Write-Host "  its Start menu and Installed apps entries"
    if ($unsaved.Count -gt 0) { Write-Host "  and work that is not on GitHub: $($unsaved -join '; ')" }
    if (-not $NoKeepData -and $keep.Count -gt 0) { Write-Host "A copy of $($keep -join ', ') is kept in your Documents folder." }
    Write-Host "Kept: .flightrisk, .flightrisk-fleet.env, Python and git."
    $answer = Read-Host "Type yes to uninstall"
    if ($answer -ne "yes") { Write-Host "Cancelled; nothing was changed."; exit 1 }
}

$keptDir = $null
if (-not $alreadyGone -and -not $NoKeepData -and $keep.Count -gt 0) {
    $documents = [Environment]::GetFolderPath("MyDocuments")
    if (-not $documents) { $documents = Join-Path $HOME "Documents" }
    $keptDir = Join-Path $documents ("FlightRisk data " + (Get-Date -Format "yyyy-MM-dd-HHmmss"))
    New-Item -ItemType Directory -Force -Path $keptDir | Out-Null
    foreach ($name in $keep) {
        try {
            Copy-Item -LiteralPath (Join-Path $AppDir $name) -Destination $keptDir -Recurse -Force
        } catch {
            Write-Host "uninstall.ps1: could not copy $name to $keptDir; nothing was deleted." -ForegroundColor Red
            exit 1
        }
    }
}

$shortcutRemoved = $false
if (Test-OurShortcut) { Remove-Item -LiteralPath (Get-StartMenuShortcut) -Force; $shortcutRemoved = $true }
$unregistered = $false
if (Test-OurRegistration) { Remove-Item -Path $UninstallKey -Recurse -Force; $unregistered = $true }

if (-not $alreadyGone) {
    # Moved aside first, so a delete that stops half-way (a file still open) never
    # leaves a broken install where the Start menu entry looks for one.
    $trash = Join-Path $FlightRiskHome ".removing-$PID"
    try {
        Move-Item -LiteralPath $AppDir -Destination $trash
    } catch {
        Write-Host "uninstall.ps1: $AppDir is in use by another program, so it was not deleted. Close it and run this again." -ForegroundColor Red
        exit 3
    }
    Remove-Item -LiteralPath $trash -Recurse -Force
}
foreach ($name in @("install-status.json", "install-status.json.lock")) {
    Remove-Item -LiteralPath (Join-Path $FlightRiskHome $name) -Force -ErrorAction SilentlyContinue
}
if ((Test-Path -LiteralPath $FlightRiskHome) -and -not (Get-ChildItem -LiteralPath $FlightRiskHome -Force | Select-Object -First 1)) {
    Remove-Item -LiteralPath $FlightRiskHome -Force
}

[ordered]@{
    removed         = $true
    alreadyGone     = $alreadyGone
    installDir      = $AppDir
    shortcutRemoved = $shortcutRemoved
    unregistered    = $unregistered
    keptData        = $keptDir
} | ConvertTo-Json -Compress
Write-Host "FlightRisk was uninstalled. .flightrisk and .flightrisk-fleet.env were kept."
