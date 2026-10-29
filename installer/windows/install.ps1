# FlightRisk -- per-user installer for Windows (the edge laptop).
#
#   irm https://raw.githubusercontent.com/KurtLehnardt/flightRisk/main/installer/windows/install.ps1 | iex
#
# or, from a downloaded copy:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#
# What it does, each step skipped when it is already done, so it is safe to run
# again (that is also how an install is repaired or updated). Nothing needs an
# administrator:
#
#   1. Installs Python 3.13 and git for this user with winget if they are missing.
#   2. Downloads FlightRisk into %LOCALAPPDATA%\FlightRisk\app: the newest release
#      (a tag like v1.2.3), or main while no release exists. $env:FLIGHTRISK_REF
#      picks one. An existing install is moved to it, but never backwards, never
#      over local changes, never while FlightRisk is running, and never when the
#      folder was not made by this installer.
#   3. Creates the virtual environment app\venv and installs the pinned edge
#      packages of installer\locks\windows-x64-edge.txt (PyTorch, Ultralytics,
#      OpenCV, djitellopy; this laptop runs edges only).
#   4. Downloads the yolo11n detector weights.
#   5. Adds a FlightRisk entry to the Start menu (it opens the launcher window) and
#      to Settings > Apps > Installed apps (for uninstalling).
#
# It never asks for, stores or prints the edge token; the launcher reads it from
# FLIGHTRISK_EDGE_TOKEN or %USERPROFILE%\.flightrisk-fleet.env. The steps that need
# an administrator (the static address of the cable adapter, firewall rules,
# power settings) stay manual: see docs\windows-edge-setup.md, steps 3 and 7.
#
# Progress goes to a status file, by default %LOCALAPPDATA%\FlightRisk\install-status.json,
# as one JSON object with state ("running", "done" or "error"), step, message,
# ref, pid and at. A lock file next to it, held open while the install runs,
# keeps two installs from running at once.
#
# Settings (environment variables, all optional):
#   FLIGHTRISK_REF               release tag (v1.2.3) or main; default: the newest release
#   FLIGHTRISK_HOME              install root; default %LOCALAPPDATA%\FlightRisk
#   FLIGHTRISK_PYTHON            a Python 3.13 to build the virtual environment with
#   FLIGHTRISK_STATUS_FILE       where the status JSON goes
#   FLIGHTRISK_REPO_URL          the repository to install from
#   FLIGHTRISK_ALLOW_DOWNGRADE=1 allow moving an install to an older release
#   FLIGHTRISK_INSTALL_SKIP_DEPS=1  install no packages and download no weights
#   FLIGHTRISK_NO_SHORTCUTS=1    add no Start menu entry and no Installed apps entry

$ErrorActionPreference = "Stop"

$script:FlightRiskHome = if ($env:FLIGHTRISK_HOME) { $env:FLIGHTRISK_HOME } else { Join-Path $env:LOCALAPPDATA "FlightRisk" }
$script:AppDir = Join-Path $script:FlightRiskHome "app"
$script:VenvDir = Join-Path $script:AppDir "venv"
$script:StatusPath = if ($env:FLIGHTRISK_STATUS_FILE) { $env:FLIGHTRISK_STATUS_FILE } else { Join-Path $script:FlightRiskHome "install-status.json" }
$script:RepoUrl = if ($env:FLIGHTRISK_REPO_URL) { $env:FLIGHTRISK_REPO_URL } else { "https://github.com/KurtLehnardt/flightRisk.git" }
$script:MarkerName = "flightrisk-installer"
$script:PythonWanted = "3.13"
$script:LockFileWindows = "installer\locks\windows-x64-edge.txt"
$script:UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlightRisk"
$script:Step = "start"
$script:Ref = $null
$script:PrevHead = $null
$script:Moved = $false
$script:CreatedAppDir = $false
$script:LockStream = $null
$script:PythonExe = $null
$script:Died = $false

function Log([string]$Message) { Write-Host ""; Write-Host $Message -ForegroundColor White }
function Ok([string]$Message) { Write-Host "  [ok] $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "  [!] $Message" -ForegroundColor Yellow }

# The status object as one line of JSON (no byte order mark, in both PowerShell 5.1 and 7).
function Get-StatusJson([string]$State, [string]$Message) {
    $status = [ordered]@{
        state   = $State
        step    = $script:Step
        message = if ($Message) { $Message } else { $null }
        ref     = $script:Ref
        pid     = $PID
        at      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    return ($status | ConvertTo-Json -Compress)
}

function Write-Status([string]$State, [string]$Message) {
    try {
        $dir = Split-Path -Parent $script:StatusPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $tmp = "$($script:StatusPath).$PID.tmp"
        [System.IO.File]::WriteAllText($tmp, (Get-StatusJson $State $Message), (New-Object System.Text.UTF8Encoding $false))
        Move-Item -LiteralPath $tmp -Destination $script:StatusPath -Force
    } catch {
        Write-Host "  [!] Could not write the install status to $($script:StatusPath)" -ForegroundColor Yellow
    }
}

# Stop the install with $Message. A script run from a file exits with code 1;
# one run with "irm ... | iex" throws instead, because exit would close the
# PowerShell window and the message with it.
function Die([string]$Message) {
    Undo-Move
    Write-Status "error" $Message
    Write-Host "  [x] $Message" -ForegroundColor Red
    $script:Died = $true
    if ($PSCommandPath) { exit 1 }
    throw "FlightRisk install failed: $Message"
}

function Set-Step([string]$Name, [string]$Message) {
    $script:Step = $Name
    Log $Message
    Write-Status "running" $Message
}

# Hold "<status>.lock" open with no sharing; Windows releases it when this process ends.
function Enter-InstallLock {
    $lockPath = "$($script:StatusPath).lock"
    $dir = Split-Path -Parent $lockPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    try {
        $script:LockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        return $true
    } catch [System.IO.IOException] {
        return $false
    }
}

function Exit-InstallLock {
    if ($script:LockStream) { $script:LockStream.Dispose(); $script:LockStream = $null }
}

# The newest release tag (vX.Y.Z only) among "git ls-remote --tags" lines, or $null.
function Select-LatestTag([string[]]$Lines) {
    $tags = foreach ($line in $Lines) {
        if ($line -match '\trefs/tags/(v(\d+)\.(\d+)\.(\d+))$') {
            [pscustomobject]@{ Name = $Matches[1]; Version = [version]::new([int]$Matches[2], [int]$Matches[3], [int]$Matches[4]) }
        }
    }
    $newest = $tags | Sort-Object -Property Version | Select-Object -Last 1
    if ($newest) { return $newest.Name }
    return $null
}

function Test-RefName([string]$Ref) {
    return ($Ref -ceq "main") -or ($Ref -cmatch '^v\d+\.\d+\.\d+$')
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# Re-read PATH after winget changed it, so a new git or Python is found in this window.
function Sync-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

# The major.minor version of the Python at $Exe, or $null if it does not run.
function Get-PythonVersion([string]$Exe) {
    # Native commands report failure through $LASTEXITCODE, checked below. With
    # "Stop", Windows PowerShell 5.1 would turn any text they write to stderr
    # into a terminating error.
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]$out).Trim() }
    } catch {
        return $null
    }
    return $null
}

function Get-PythonCandidates {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    if ($null -ne $env:FLIGHTRISK_PYTHON_CANDIDATES) {
        return @($env:FLIGHTRISK_PYTHON_CANDIDATES -split ';' | Where-Object { $_ })
    }
    $candidates = @()
    if (Test-Command "py") {
        try {
            $exe = & py "-$($script:PythonWanted)" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += ([string]$exe).Trim() }
        } catch {
            Write-Verbose "py -$($script:PythonWanted) is not available"
        }
    }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe") }
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "Python313\python.exe") }
    foreach ($name in @("python3.13", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    return $candidates
}

function Find-Python {
    if ($env:FLIGHTRISK_PYTHON) {
        if ((Get-PythonVersion $env:FLIGHTRISK_PYTHON) -eq $script:PythonWanted) { $script:PythonExe = $env:FLIGHTRISK_PYTHON; return $true }
        Die "FLIGHTRISK_PYTHON=$($env:FLIGHTRISK_PYTHON) is not a working Python $($script:PythonWanted)."
    }
    foreach ($candidate in Get-PythonCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate) -and (Get-PythonVersion $candidate) -eq $script:PythonWanted) {
            $script:PythonExe = $candidate
            return $true
        }
    }
    return $false
}

function Invoke-Winget([string]$Id) {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    if (-not (Test-Command "winget")) {
        Die "winget was not found, so $Id cannot be installed automatically. Install it yourself (Python 3.13 from https://www.python.org/downloads/windows/, git from https://git-scm.com/download/win) and run this again."
    }
    & winget install -e --id $Id --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Die "winget could not install $Id (exit code $LASTEXITCODE)." }
    Sync-Path
}

function Install-Git {
    if (Test-Command "git") { Ok "git found"; return }
    Log "Installing git for this user with winget..."
    Invoke-Winget "Git.Git"
    if (-not (Test-Command "git")) { Die "git is still not found after installing it. Open a new PowerShell window and run this again." }
    Ok "git installed"
}

function Install-Python {
    if (Find-Python) { Ok "Python $($script:PythonWanted) found ($($script:PythonExe))"; return }
    Log "Installing Python $($script:PythonWanted) for this user with winget..."
    Invoke-Winget "Python.Python.3.13"
    if (-not (Find-Python)) { Die "Python $($script:PythonWanted) is still not found after installing it. Open a new PowerShell window and run this again." }
    Ok "Python $($script:PythonWanted) installed ($($script:PythonExe))"
}

function Invoke-Git {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed with exit code $LASTEXITCODE" }
}

function Select-Ref {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    if ($env:FLIGHTRISK_REF) {
        if (-not (Test-RefName $env:FLIGHTRISK_REF)) { Die "FLIGHTRISK_REF must be a release tag like v1.2.3, or main (got '$($env:FLIGHTRISK_REF)')." }
        $script:Ref = $env:FLIGHTRISK_REF
        return
    }
    $lines = & git ls-remote --tags --refs $script:RepoUrl 2>$null
    if ($LASTEXITCODE -ne 0) { Die "Could not reach $($script:RepoUrl). Check the network connection and run this again." }
    $latest = Select-LatestTag @($lines)
    if ($latest) {
        $script:Ref = $latest
    } else {
        $script:Ref = "main"
        Warn "no release has been published yet, so the newest code on main is installed"
    }
}

# Process ids of programs running from this install's virtual environment
# (the launcher, an edge), never this one.
function Get-RunningPids {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    $venv = $script:VenvDir.TrimEnd('\', '/')
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
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

function Assert-NotRunning {
    $pids = @(Get-RunningPids)
    if ($pids.Count -gt 0) {
        Die "FlightRisk is running from $($script:AppDir) (process $($pids -join ', ')). Land the drones, stop everything in the launcher window and quit it, then run this again. The installer never stops FlightRisk itself, because an edge may be flying a drone."
    }
}

function Write-Marker {
    $marker = Join-Path (Join-Path $script:AppDir ".git") $script:MarkerName
    $text = "installer=installer/windows/install.ps1`nref=$($script:Ref)`ndate=$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))`n"
    [System.IO.File]::WriteAllText($marker, $text, (New-Object System.Text.UTF8Encoding $false))
}

function Get-MarkerRef {
    $marker = Join-Path (Join-Path $script:AppDir ".git") $script:MarkerName
    foreach ($line in (Get-Content -LiteralPath $marker -ErrorAction SilentlyContinue)) {
        if ($line -match '^ref=(.+)$') { return $Matches[1] }
    }
    return $null
}

# Fetch the release into the clone; returns the git object to check out.
function Get-RefTarget {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    if ($script:Ref -eq "main") {
        & git -C $script:AppDir fetch --quiet --force origin "+refs/heads/main:refs/remotes/origin/main"
        if ($LASTEXITCODE -ne 0) { Die "Could not download the newest FlightRisk (git fetch failed)." }
        return "refs/remotes/origin/main"
    }
    & git -C $script:AppDir fetch --quiet --force origin "+refs/tags/$($script:Ref):refs/tags/$($script:Ref)"
    if ($LASTEXITCODE -ne 0) { Die "Could not download FlightRisk $($script:Ref) (git fetch failed). Is it a published release?" }
    return "refs/tags/$($script:Ref)"
}

function Invoke-Checkout([string]$Target) {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    & git -C $script:AppDir -c advice.detachedHead=false checkout --quiet --detach $Target
    return ($LASTEXITCODE -eq 0)
}

function Update-OrClone {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    $item = Get-Item -LiteralPath $script:AppDir -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        Die "$($script:AppDir) is a link to another folder. Remove the link and run this again."
    }
    if (Test-Path -LiteralPath (Join-Path $script:AppDir ".git")) {
        if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $script:AppDir ".git") $script:MarkerName))) {
            Die "$($script:AppDir) was not made by this installer (your own checkout?), so it was not changed."
        }
        Assert-NotRunning
        $changes = & git -C $script:AppDir status --porcelain --untracked-files=no
        if ($changes) { Die "$($script:AppDir) has local changes, so it was not changed. Save or undo them first." }
        Invoke-Git -C $script:AppDir remote set-url origin $script:RepoUrl
        $target = Get-RefTarget
        $head = (& git -C $script:AppDir rev-parse HEAD).Trim()
        $want = (& git -C $script:AppDir rev-parse "$target^{commit}").Trim()
        if ($head -eq $want) {
            Ok "already at $($script:Ref)"
        } else {
            & git -C $script:AppDir merge-base --is-ancestor $want $head
            $isOlder = ($LASTEXITCODE -eq 0)
            if ($isOlder -and $env:FLIGHTRISK_ALLOW_DOWNGRADE -ne "1") {
                Warn "not moving to $($script:Ref): the installed version is newer (set FLIGHTRISK_ALLOW_DOWNGRADE=1 to go back)"
                $script:Ref = Get-MarkerRef
                return
            }
            $script:PrevHead = $head
            $script:Moved = $true
            if (-not (Invoke-Checkout $target)) { Die "Could not switch $($script:AppDir) to $($script:Ref) (git checkout failed)." }
            Ok "moved to $($script:Ref)"
        }
        Write-Marker
        return
    }
    if ((Test-Path -LiteralPath $script:AppDir) -and (Get-ChildItem -LiteralPath $script:AppDir -Force | Select-Object -First 1)) {
        Die "$($script:AppDir) exists but is not a FlightRisk install made by this installer, so it was not changed."
    }
    Log "Downloading FlightRisk $($script:Ref) into $($script:AppDir) ..."
    New-Item -ItemType Directory -Force -Path $script:FlightRiskHome | Out-Null
    $script:CreatedAppDir = $true
    & git clone --quiet --no-checkout $script:RepoUrl $script:AppDir
    if ($LASTEXITCODE -ne 0) { Die "git clone of $($script:RepoUrl) failed. Check the network connection and run this again." }
    $target = Get-RefTarget
    if (-not (Invoke-Checkout $target)) { Die "Could not check out FlightRisk $($script:Ref). Is it a published release?" }
    Write-Marker
    $script:CreatedAppDir = $false
    Ok "downloaded $($script:Ref)"
}

# After a failure: put an install that this run moved back where it was, and
# remove a folder this run created but could not finish.
function Undo-Move {
    if ($script:Moved -and $script:PrevHead) {
        $script:Moved = $false
        if (Invoke-Checkout $script:PrevHead) { Warn "put $($script:AppDir) back on the version it had before; run the installer again to retry" }
    }
    if ($script:CreatedAppDir) {
        $script:CreatedAppDir = $false
        Remove-Item -LiteralPath $script:AppDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-VenvPython {
    if ($IsWindows -or $env:OS -eq "Windows_NT") { return (Join-Path $script:VenvDir "Scripts\python.exe") }
    return (Join-Path $script:VenvDir "bin/python")
}

function New-Venv {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    $venvPython = Get-VenvPython
    if ((Test-Path -LiteralPath $venvPython) -and (Get-PythonVersion $venvPython) -eq $script:PythonWanted) {
        Ok "virtual environment found ($($script:VenvDir))"
        return
    }
    if (Test-Path -LiteralPath $script:VenvDir) {
        Warn "the virtual environment is not Python $($script:PythonWanted); making a new one"
        Remove-Item -LiteralPath $script:VenvDir -Recurse -Force
    }
    & $script:PythonExe -m venv $script:VenvDir
    if ($LASTEXITCODE -ne 0) { Die "Could not create the virtual environment in $($script:VenvDir)." }
    Ok "virtual environment created ($($script:VenvDir))"
}

# The requirements file to install from: the pinned lock on 64-bit Windows,
# else requirements-edge.txt with its version floors only.
function Get-RequirementsFile {
    $lock = Join-Path $script:AppDir $script:LockFileWindows
    if ($env:PROCESSOR_ARCHITECTURE -eq "AMD64" -and (Test-Path -LiteralPath $lock)) { return $script:LockFileWindows }
    return "requirements-edge.txt"
}

function Install-Dependencies {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    if ($env:FLIGHTRISK_INSTALL_SKIP_DEPS -eq "1") {
        Warn "FLIGHTRISK_INSTALL_SKIP_DEPS=1: no packages installed and no weights downloaded"
        return
    }
    $req = Get-RequirementsFile
    if ($req -eq "requirements-edge.txt") { Warn "no pinned package list for this computer; installing requirements-edge.txt (version floors only)" }
    $venvPython = Get-VenvPython
    $stamp = Join-Path $script:VenvDir ".flightrisk-deps"
    $sum = "$req $((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $script:AppDir $req)).Hash.ToLowerInvariant())"
    if ((Test-Path -LiteralPath $stamp) -and ((Get-Content -LiteralPath $stamp -Raw).Trim() -eq $sum)) {
        Ok "packages already installed from $req"
        return
    }
    Log "Installing the Python packages from $req (several GB; this takes a while)..."
    & $venvPython -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) { Die "Upgrading pip failed (exit code $LASTEXITCODE)." }
    Push-Location $script:AppDir
    try {
        & $venvPython -m pip install --require-virtualenv -r $req
        if ($LASTEXITCODE -ne 0) { Die "Installing the packages from $req failed (exit code $LASTEXITCODE)." }
    } finally {
        Pop-Location
    }
    [System.IO.File]::WriteAllText($stamp, $sum, (New-Object System.Text.UTF8Encoding $false))
    Ok "packages installed"
}

function Get-Weights {
    $ErrorActionPreference = "Continue"  # see Get-PythonVersion
    if ($env:FLIGHTRISK_INSTALL_SKIP_DEPS -eq "1") { return }
    if (Test-Path -LiteralPath (Join-Path $script:AppDir "yolo11n.pt")) { Ok "yolo11n.pt found"; return }
    Push-Location $script:AppDir
    try {
        & (Get-VenvPython) -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" | Out-Null
        if ($LASTEXITCODE -eq 0) { Ok "yolo11n.pt downloaded" }
        else { Warn "could not download yolo11n.pt now; Ultralytics downloads it the first time an edge starts" }
    } finally {
        Pop-Location
    }
}

function Get-StartMenuShortcut {
    return (Join-Path ([Environment]::GetFolderPath("Programs")) "FlightRisk.lnk")
}

# The Start menu entry (it opens scripts\launcher.bat, which keeps a console
# window with the launcher's address) and the Installed apps entry.
function Register-App {
    if ($env:FLIGHTRISK_NO_SHORTCUTS -eq "1") { Warn "FLIGHTRISK_NO_SHORTCUTS=1: no Start menu or Installed apps entry"; return }
    if (-not ($IsWindows -or $env:OS -eq "Windows_NT")) { Warn "not Windows: no Start menu or Installed apps entry"; return }
    $icon = Join-Path $script:AppDir "installer\assets\FlightRisk.ico"
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Get-StartMenuShortcut))
    $link.TargetPath = Join-Path $script:AppDir "scripts\launcher.bat"
    $link.WorkingDirectory = $script:AppDir
    $link.IconLocation = "$icon,0"
    $link.Description = "Open the FlightRisk launcher window"
    $link.Save()
    Ok "added FlightRisk to the Start menu"

    $uninstall = Join-Path $script:AppDir "installer\windows\uninstall.ps1"
    $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    New-Item -Path $script:UninstallKey -Force | Out-Null
    $values = @{
        DisplayName          = "FlightRisk"
        DisplayVersion       = $script:Ref
        Publisher            = "Kurt Lehnardt"
        InstallLocation      = $script:AppDir
        DisplayIcon          = $icon
        UninstallString      = "`"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$uninstall`""
        QuietUninstallString = "`"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$uninstall`" -Yes"
    }
    foreach ($name in $values.Keys) { Set-ItemProperty -Path $script:UninstallKey -Name $name -Value $values[$name] }
    Set-ItemProperty -Path $script:UninstallKey -Name NoModify -Value 1 -Type DWord
    Set-ItemProperty -Path $script:UninstallKey -Name NoRepair -Value 1 -Type DWord
    Ok "added FlightRisk to Settings > Apps > Installed apps"
}

function Invoke-Install {
    New-Item -ItemType Directory -Force -Path $script:FlightRiskHome | Out-Null
    if (-not (Enter-InstallLock)) {
        Write-Host "  [x] Another FlightRisk install is running. Wait for it to finish." -ForegroundColor Red
        exit 1
    }
    try {
        Write-Status "running" ""
        Log "FlightRisk -- Windows install into $($script:FlightRiskHome)"

        Set-Step "prerequisites" "Checking git and Python $($script:PythonWanted)"
        Install-Git
        Install-Python

        Set-Step "download" "Choosing the release"
        Select-Ref
        Ok "release: $($script:Ref)"
        Update-OrClone

        Set-Step "venv" "Setting up the virtual environment"
        New-Venv

        Set-Step "packages" "Installing the Python packages"
        Install-Dependencies

        Set-Step "weights" "Detector weights"
        Get-Weights

        Set-Step "shortcuts" "Start menu and Installed apps"
        Register-App

        $script:Moved = $false
        $script:Step = "done"
        Write-Status "done" ""
        Log "Done. FlightRisk $($script:Ref) is installed in $($script:AppDir)."
        Write-Host "  Open FlightRisk from the Start menu (it opens the launcher window)."
        Write-Host "  The edge token is read from FLIGHTRISK_EDGE_TOKEN or %USERPROFILE%\.flightrisk-fleet.env; this installer never asks for it."
        Write-Host "  Still manual (administrator): the cable adapter's address and the firewall rules, docs\windows-edge-setup.md steps 3 and 7."
        Write-Host "  Update:    powershell -NoProfile -ExecutionPolicy Bypass -File `"$($script:AppDir)\installer\windows\update.ps1`""
        Write-Host "  Uninstall: Settings > Apps > Installed apps > FlightRisk"
    } catch {
        if ($script:Died) { throw }
        Die "Failed: $($_.Exception.Message)"
    } finally {
        Exit-InstallLock
    }
}

# Tests dot-source this file with FLIGHTRISK_INSTALL_LIB_ONLY=1 to call the
# functions above one at a time.
if ($env:FLIGHTRISK_INSTALL_LIB_ONLY -eq "1") { return }
Invoke-Install
