# Installer and FlightRisk.app

FlightRisk installs per user, with no administrator rights, from a pinned release of this repository,
and opens the [launcher window](launcher.md) from an app: `FlightRisk.app` on the Mac and a Start menu
entry on Windows. The design follows the installer of Granted (KurtLehnardt/granted), adapted to a
Python app whose dependencies weigh several GB.

## Install

### macOS (Apple Silicon, macOS 14 or newer)

From the disk image: open `FlightRisk-x.y.z-arm64.dmg`, drag FlightRisk to Applications and open it.
FlightRisk offers to install itself; the install runs in a Terminal window that shows its progress, and
FlightRisk opens the launcher window when it is done.

From the command line (the same installer):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KurtLehnardt/flightRisk/main/installer/macos/install.sh)"
```

Then open FlightRisk from `~/Applications` or Spotlight. Run the same command again to repair an install.

**Gatekeeper, the first time.** The app in the disk image is not signed with an Apple Developer ID, so
macOS blocks its first launch after a download. Recent macOS versions removed the right-click, Open
shortcut, so this is the way through:

1. Open FlightRisk from Applications. macOS says it was blocked.
2. Open System Settings, Privacy & Security, and scroll down to Security. It names FlightRisk as
   blocked, with an **Open Anyway** button.
3. Click **Open Anyway** and confirm with Touch ID or your password. From then on FlightRisk opens
   normally.

Fallback for a file you downloaded yourself and trust: `xattr -dr com.apple.quarantine /Applications/FlightRisk.app`.
The `~/Applications/FlightRisk.app` that the command-line installer writes is made on your Mac, carries no
download flag and opens without this step.

### Windows (64-bit, the edge laptop)

In PowerShell:

```powershell
irm https://raw.githubusercontent.com/KurtLehnardt/flightRisk/main/installer/windows/install.ps1 | iex
```

or run `FlightRisk-Setup-x.y.z.exe`, which runs the same script in a PowerShell window. Then open
FlightRisk from the Start menu. The Windows install sets up the edge role only (`requirements-edge.txt`);
the steps that need an administrator stay manual: the cable adapter's address, the firewall rules and the
power settings in [windows-edge-setup.md](windows-edge-setup.md), steps 3 and 7.

**SmartScreen.** The Setup .exe is not code-signed, so Windows may say "Windows protected your PC". Click
**More info**, then **Run anyway**.

### The edge token

No installer asks for, stores or prints the edge token. The launcher reads it from
`FLIGHTRISK_EDGE_TOKEN` or `~/.flightrisk-fleet.env` (`%USERPROFILE%\.flightrisk-fleet.env` on Windows),
as described in [fleet-setup.md](fleet-setup.md).

## What gets installed where

| | macOS | Windows |
| --- | --- | --- |
| Install root | `~/Library/Application Support/FlightRisk` | `%LOCALAPPDATA%\FlightRisk` |
| Checkout of the release | `app/` (a git clone, detached at the release tag) | `app\` |
| Virtual environment | `app/venv` (Python 3.13) | `app\venv` |
| Packages | `installer/locks/macos-arm64.txt` (full app) | `installer\locks\windows-x64-edge.txt` (edge role) |
| Detector weights | `app/yolo11n.pt` | `app\yolo11n.pt` |
| Marker | `app/.git/flightrisk-installer` | `app\.git\flightrisk-installer` |
| Progress | `install-status.json` and the lock `install.lock.d/` | `install-status.json` and `install-status.json.lock` |
| App | `~/Applications/FlightRisk.app` | Start menu `FlightRisk` (opens `scripts\launcher.bat`) |
| Uninstall entry | `installer/macos/uninstall.sh` | Settings > Apps > Installed apps > FlightRisk |
| Logs of the app | `~/Library/Logs/FlightRisk/` | the launcher's console window |

`~/.flightrisk` (presets, the saved ceiling) and `~/.flightrisk-fleet.env` are never written or removed by
the installer, the updater or the uninstaller. The fine-tuned HERIDAL weights
(`models/heridal_yolo11s.pt`) are not published; copy them into `app/models/` to offer the `top_down`
detector profile.

## Update and uninstall

| | macOS | Windows |
| --- | --- | --- |
| Is there an update? | `bash ".../app/installer/macos/update.sh" --check` | `update.ps1 -Check` |
| Update to the newest release | `bash ".../app/installer/macos/update.sh"` | `update.ps1` |
| Uninstall | `bash ".../app/installer/macos/uninstall.sh"` | Installed apps, or `uninstall.ps1` |

An update is the installer run again for the new release. It refuses a folder the installer did not make
(no marker), local changes, and a running FlightRisk; it never moves an install backwards
(`FLIGHTRISK_ALLOW_DOWNGRADE=1` overrides); and when installing the new packages fails, it checks the
previous release out again. The installer, updater and uninstaller **never stop FlightRisk themselves**,
because an edge may be flying a drone: they name the running processes and ask you to land, stop and
quit from the launcher window first.

The uninstaller removes the install folder, the app (only when its bundle id is FlightRisk's), the Start
menu and Installed apps entries (only when they point into this install) and the logs. It keeps a copy of
what FlightRisk wrote into the install folder (`logs/`, `captures/`, `recordings/`, `models/`,
`flightrisk_sessions.db`, `target_reference.*`) in `Documents/FlightRisk data <date>/` unless
`--no-keep-data` (`-NoKeepData`) is given; captures can show people, so delete that copy when you no
longer need it. It refuses, deleting nothing: a link (exit 2), a folder with no marker even with
`--force` (exit 2), a running FlightRisk (exit 3), and work that is not on GitHub without `--force`
(exit 4). `--check` (`-Check`) prints what it would do as one JSON line.

## Releases and builds

- **Release tags.** The installers install the newest `vX.Y.Z` tag (pre-release tags such as
  `v1.0.0-rc1` are ignored) and fall back to `main` while no release exists. `FLIGHTRISK_REF=v1.2.3` or
  `FLIGHTRISK_REF=main` picks one.
- **Disk image.** `bash installer/macos/build-dmg.sh --ref v1.2.3` writes `dist/FlightRisk-1.2.3-arm64.dmg`
  with FlightRisk.app (ad-hoc signed), an Applications shortcut and a "Read me first" note. The app's
  bundled installer installs the release given with `--ref`.
- **Windows Setup.** On Windows with Inno Setup 6:
  `iscc /DAppVersion=1.2.3 /DAppRef=v1.2.3 installer\windows\FlightRisk.iss` writes
  `dist\FlightRisk-Setup-1.2.3.exe`.
- **Pinned packages.** `bash installer/update-locks.sh` regenerates both lock files with `uv` from a working
  virtual environment; commit them with the change to `requirements*.txt` that needed it.
- **Icon.** `python3 installer/assets/make_icon.py` draws the placeholder icon from code and writes
  `FlightRisk.icns` and `FlightRisk.ico`.

## Design

### What Granted ships, and what FlightRisk takes from it

| Granted | FlightRisk | Why |
| --- | --- | --- |
| One-shot, re-runnable shell installers per OS (`install-macos.sh`, `install-windows.ps1`) that skip finished steps | Taken: `installer/macos/install.sh`, `installer/windows/install.ps1` | Re-running is also the repair and update path. |
| `bash -c "$(curl ...)"`, never `curl ... \| bash` | Taken | Homebrew reads standard input and can swallow the rest of a piped script. |
| Homebrew (pinned installer commit and checksum), git and the runtime installed when missing; winget on Windows | Taken, with Python 3.13 as the runtime; winget installs with `--scope user` | The same prerequisites story, and no administrator on Windows. |
| Release tag pinning, never backwards, never over local changes or someone's own checkout (marker in `.git`) | Taken | The same safety rules, plus "never while FlightRisk runs". |
| Status file (`{state, message, pid}`), lock directory on macOS and held file on Windows, `ERR` trap | Taken, with `step`, `ref` and `at` added | Progress is machine-readable; two installs cannot overlap. |
| Per-user launcher app `~/Applications/Granted.app` (shell script bundle, `LSUIElement`, bundle id check before replacing) that starts the server and opens it in its own window | Taken: `FlightRisk.app` starts the launcher in the background and opens its window (Chrome or Edge app mode when present) | The launcher is already a local web page; the app only has to start it and open it. |
| Update and uninstall scripts with refusals, `--check`, keeping the user's keys | Taken; "keys" become the data FlightRisk writes into its folder | The token already lives outside the install. |
| Electron GUI installer (`Granted Setup` .dmg, Windows Setup .exe), Playwright tests | **Not taken.** The .dmg holds FlightRisk.app itself, which offers the install and runs it in Terminal; Windows gets a small Inno Setup bootstrapper | See below. |
| Menu-bar helper (Swift), LaunchAgent, Windows tray, in-app auto-update | **Not taken** | The launcher window already shows status, logs and Quit; FlightRisk has no settings page to host an updater. |

### Frozen bundle or bootstrapped environment

Two ways to package a Python app were weighed:

- **(a) A frozen bundle** (PyInstaller or similar) of the whole app. PyTorch, Ultralytics, OpenCV,
  MAVSDK (which ships its own `mavsdk_server` binary) and ONNX Runtime together make a bundle of several
  GB that must be rebuilt and re-downloaded in full for every change, per platform. Freezing torch is
  fragile (hidden imports, dynamic libraries, MPS on macOS, CPU builds on Windows), the launcher starts
  the ground station and edges as `python -m ...` child processes, which a frozen app would have to
  re-implement, and an unsigned multi-GB bundle meets Gatekeeper and SmartScreen just the same.
- **(b) A small native app that bootstraps its own virtual environment** from a pinned release and opens
  the launcher. The app is about 200 KB; the packages come from PyPI at pinned versions, an update
  downloads only what changed, and the code that runs is exactly the repository at a release tag, so
  `scripts/launcher.command` and every documented command keep working inside the install.

**Chosen: (b).** The cost is that the first start needs the network and a few minutes, which the
Terminal window makes visible.

### Why no GUI installer

Granted's Electron installer guides people who have never used a terminal through keys and local
models. FlightRisk has two known machines, no keys to collect (the token is deliberately kept out of the
installer) and nothing to choose at install time. An Electron app would add Node, about 100 MB of
Electron and its own test stack to a Python repository for one progress screen. The Terminal window
(macOS) and the PowerShell window (Windows) show the progress, the status file reports it to anything
that wants to watch, and the launcher window is the GUI from then on.

### Other choices

- **Pinned packages.** `requirements.txt` has version floors only. The installers install from lock
  files resolved by `uv` against the versions the development environment runs (`installer/locks/`), so a
  new install gets tested versions. A Mac that is not Apple Silicon with macOS 14 falls back to
  `requirements.txt`.
- **Windows runs edges only.** The Windows lock is `requirements-edge.txt`, as `scripts/install_windows.ps1`
  already does; InsightFace, Ollama, open_clip and MediaPipe are never installed there.
- **The Start menu entry opens `scripts\launcher.bat`.** It keeps the console window that the launcher
  documentation already describes (the address, Ctrl+C), so the Windows behaviour stays the one that the
  launcher was built and documented for.
- **The developer setup scripts stay.** `scripts/install_macos.sh`, `scripts/install_windows.ps1` and
  `scripts/install_linux.sh` set up a checkout you cloned yourself (with Ollama models and the test
  suite); `installer/` installs FlightRisk as an app for someone who only wants to run it.

## Not verified

- Homebrew, git or Python 3.13 installed from scratch on a fresh Mac (tests replace Homebrew with a fake;
  the pinned Homebrew installer is the one Granted verified).
- The real package install from the lock files (several GB); tests use `FLIGHTRISK_INSTALL_SKIP_DEPS=1`.
- Opening FlightRisk.app from Finder, the install dialog and the Terminal bootstrap from the .dmg, and the
  Chrome or Edge app window (tests run the app's program directly with `FLIGHTRISK_OPEN_IN=none`).
- Clicking **Open Anyway**: a quarantined copy of the built app was confirmed to be rejected by Gatekeeper
  (`spctl` reports "rejected"), as the steps above expect.
- Everything on Windows: winget, the Start menu entry, the Installed apps entry, `Get-CimInstance` process
  detection, Windows PowerShell 5.1, and the Inno Setup build. The PowerShell scripts were run under
  PowerShell 7 on macOS (install, update and uninstall into a temporary folder) and checked with
  PSScriptAnalyzer.
- Apple Developer ID signing, notarization and Windows code signing.
