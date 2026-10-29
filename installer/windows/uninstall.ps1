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
# virtual environment), then the Start menu entry (only when it points into this
# install) and the Installed apps entry (only when it names this install), and
# the files the installer writes next to the folder, by name.
#
# What it keeps: %USERPROFILE%\.flightrisk (launcher presets, the saved ceiling),
# %USERPROFILE%\.flightrisk-fleet.env (the edge token), Python and git. Before the
# folder is deleted, what FlightRisk wrote into it (logs, captures, recordings,
# models, flightrisk_sessions.db, target_reference.*) is copied to
# %LOCALAPPDATA%\FlightRisk data\<date>, unless -NoKeepData is given. That folder
# stays on this computer (it is not in Documents, which OneDrive may sync);
# captures can show people, so delete the copy when you no longer need it.
#
# What it refuses (and then deletes nothing), with the same exit codes as the macOS script:
#   2  the folder is a link, is not a FlightRisk checkout, or has no
#      .git\flightrisk-installer marker (someone's own checkout is never deleted,
#      not even with -Force); or FLIGHTRISK_HOME is a shared folder
#   3  FlightRisk is running from the folder, an install or update is running, or
#      the folder is in use (it never stops FlightRisk, because an edge may be
#      flying a drone; quit it from the launcher window)
#   4  the folder holds changes that are not on GitHub, and -Force was not given
#   1  cancelled at the question
param(
    [switch]$Yes,
    [switch]$Force,
    [switch]$Check,
    [switch]$NoKeepData
)

$ErrorActionPreference = "Stop"

$FlightRiskHome = $(if ($env:FLIGHTRISK_HOME) { $env:FLIGHTRISK_HOME } else { Join-Path $env:LOCALAPPDATA "FlightRisk" }).TrimEnd('\', '/')
$AppDir = Join-Path $FlightRiskHome "app"
$Marker = Join-Path (Join-Path $AppDir ".git") "flightrisk-installer"
$StatusPath = Join-Path $FlightRiskHome "install-status.json"
$KeepRoot = Join-Path (Split-Path -Parent $FlightRiskHome) "FlightRisk data"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlightRisk"
$OnWindows = ($IsWindows -or $env:OS -eq "Windows_NT")
$KeepNames = @("logs", "captures", "recordings", "models", "flightrisk_sessions.db", "flightrisk_sessions.db-wal", "flightrisk_sessions.db-shm")

# --- shared with uninstall.ps1: begin (a test checks that both copies match) ---

# Whether the folder $Dir would put the install in a shared place.
function Test-UnsafeHome([string]$Dir) {
    $d = $Dir.TrimEnd('\', '/')
    $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    $shared = @("", $userHome, $env:LOCALAPPDATA, $env:APPDATA, $env:SystemDrive, $env:SystemRoot) |
        Where-Object { $null -ne $_ } | ForEach-Object { $_.TrimEnd('\', '/') }
    return ($d -eq "" -or $d -eq "/" -or $d -match '^[A-Za-z]:$' -or ($shared -contains $d))
}

# Process ids of programs that run FlightRisk from the install folder $AppDir,
# never this process, its parents or its children. On Windows: an executable
# inside the folder (the virtual environment's python.exe) or the folder on the
# command line. Elsewhere (the tests run on macOS): the folder on the command
# line, or a working directory or loaded file inside it.
function Get-FlightRiskPids([string]$AppDir) {
    $ErrorActionPreference = "Continue"
    $app = $AppDir.TrimEnd('\', '/')
    $onWindows = ($IsWindows -or $env:OS -eq "Windows_NT")
    $parent = @{}
    $command = @{}
    $executable = @{}
    if ($onWindows) {
        foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
            $parent[[int]$p.ProcessId] = [int]$p.ParentProcessId
            $command[[int]$p.ProcessId] = [string]$p.CommandLine
            $executable[[int]$p.ProcessId] = [string]$p.ExecutablePath
        }
    } else {
        foreach ($line in @(& ps -axo "pid=,ppid=" 2>$null)) {
            if ($line -match '^\s*(\d+)\s+(\d+)') { $parent[[int]$Matches[1]] = [int]$Matches[2] }
        }
        foreach ($line in @(& ps -axww -o "pid=,args=" 2>$null)) {
            if ($line -match '^\s*(\d+)\s+(.*)$') { $command[[int]$Matches[1]] = $Matches[2] }
        }
        $cwdOrFile = @{}
        if (Test-Path -LiteralPath $app) {
            $real = (Resolve-Path -LiteralPath $app).ProviderPath
            $current = 0
            foreach ($line in @(& lsof -nP -w -a -u (& id -u) -d cwd,txt -Fpn 2>$null)) {
                if ($line.StartsWith("p")) { $current = [int]$line.Substring(1) }
                elseif ($line.StartsWith("n")) {
                    $path = $line.Substring(1)
                    foreach ($root in @($app, $real)) {
                        if ($path -eq $root -or $path.StartsWith("$root/")) { $cwdOrFile[$current] = $true }
                    }
                }
            }
        }
    }
    $mine = @{}
    $p = $PID
    while ($p -and -not $mine.ContainsKey($p)) { $mine[$p] = $true; $p = $parent[$p] }
    foreach ($candidate in @($parent.Keys)) {
        $p = $candidate
        for ($n = 0; $p -and $n -lt 100; $n++) {
            if ($p -eq $PID) { $mine[$candidate] = $true; break }
            $p = $parent[$p]
        }
    }
    $sep = if ($onWindows) { "\" } else { "/" }
    $found = foreach ($candidate in @($parent.Keys)) {
        if ($mine.ContainsKey($candidate)) { continue }
        $cmd = [string]$command[$candidate]
        $exe = [string]$executable[$candidate]
        $hit = ($cmd.IndexOf("$app$sep", [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -or
            ($exe -and $exe.StartsWith("$app$sep", [System.StringComparison]::OrdinalIgnoreCase)) -or
            ((-not $onWindows) -and $cwdOrFile -and $cwdOrFile.ContainsKey($candidate))
        if ($hit) { $candidate }
    }
    return @($found | Sort-Object)
}

# --- shared with uninstall.ps1: end ---

function Test-IsLink([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

function Test-IsCheckout {
    return (Test-Path -LiteralPath (Join-Path $AppDir ".git")) -and (Test-Path -LiteralPath (Join-Path (Join-Path $AppDir "flightrisk") "__init__.py"))
}

# Whether an install or update holds its lock (the lock file is held open with no sharing).
function Test-InstallRunning {
    $lockPath = "$StatusPath.lock"
    if (-not (Test-Path -LiteralPath $lockPath)) { return $false }
    try {
        $stream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $stream.Dispose()
        return $false
    } catch [System.IO.IOException] {
        return $true
    }
}

# Work in the folder that is not on GitHub. Files that an interrupted update of the
# installer left changed (the marker says "pending") are the installer's, not yours.
function Get-UnsavedWork {
    $ErrorActionPreference = "Continue"
    $found = @()
    $pending = Select-String -LiteralPath $Marker -Pattern '^pending=' -Quiet -ErrorAction SilentlyContinue
    if (-not $pending) {
        $changes = @(& git -C $AppDir status --porcelain --untracked-files=no 2>$null)
        if ($changes.Count -gt 0) { $found += "changed files: $($changes.Count)" }
    }
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

if (Test-UnsafeHome $FlightRiskHome) {
    Write-Refusal "unsafe-folder" "FLIGHTRISK_HOME ($FlightRiskHome) is a shared folder; nothing was deleted." 2
}

$exists = Test-Path -LiteralPath $AppDir
$isLink = Test-IsLink $AppDir
$unsaved = @()
$keep = @()
if ($exists -and -not $isLink) {
    $unsaved = @(Get-UnsavedWork)
    $keep = @(Get-KeptItems)
}
$running = @(Get-FlightRiskPids $AppDir)
$installing = Test-InstallRunning

if ($Check) {
    [ordered]@{
        check              = $true
        installDir         = $AppDir
        exists             = $exists
        isLink             = $isLink
        flightriskCheckout = ($exists -and (Test-IsCheckout))
        installerMade      = (Test-Path -LiteralPath $Marker)
        running            = $running
        installing         = $installing
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
if ($installing) {
    Write-Refusal "install-running" "A FlightRisk install or update is running. Let it finish, then run this again." 3
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
    if (-not $NoKeepData -and $keep.Count -gt 0) { Write-Host "A copy of $($keep -join ', ') is kept in $KeepRoot (captures can show people; delete it when done)." }
    Write-Host "Kept: .flightrisk, .flightrisk-fleet.env, Python and git."
    $answer = Read-Host "Type yes to uninstall"
    if ($answer -ne "yes") { Write-Host "Cancelled; nothing was changed."; exit 1 }
}

$keptDir = $null
if (-not $alreadyGone -and -not $NoKeepData -and $keep.Count -gt 0) {
    $keptDir = Join-Path $KeepRoot (Get-Date -Format "yyyy-MM-dd-HHmmss")
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

if (-not $alreadyGone) {
    # Moved aside first, so a delete that stops half-way (a file still open) never
    # leaves a broken install; the entries are removed only once the move worked,
    # so an install that stays has its uninstall entry still.
    $trash = Join-Path $FlightRiskHome ".removing-$PID"
    try {
        Move-Item -LiteralPath $AppDir -Destination $trash
    } catch {
        Write-Refusal "in-use" "$AppDir is in use by another program, so it was not deleted. Close it and run this again." 3
    }
    Remove-Item -LiteralPath $trash -Recurse -Force
}

$shortcutRemoved = $false
if (Test-OurShortcut) { Remove-Item -LiteralPath (Get-StartMenuShortcut) -Force; $shortcutRemoved = $true }
$unregistered = $false
if (Test-OurRegistration) { Remove-Item -Path $UninstallKey -Recurse -Force; $unregistered = $true }

# Only the files the installer writes, by name; then the folder, if empty.
foreach ($name in @("install-status.json", "install-status.json.lock")) {
    Remove-Item -LiteralPath (Join-Path $FlightRiskHome $name) -Force -ErrorAction SilentlyContinue
}
foreach ($incoming in @(Get-ChildItem -LiteralPath $FlightRiskHome -Directory -Force -Filter ".app-incoming.*" -ErrorAction SilentlyContinue)) {
    if (Test-Path -LiteralPath (Join-Path $incoming.FullName ".git")) { Remove-Item -LiteralPath $incoming.FullName -Recurse -Force }
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
