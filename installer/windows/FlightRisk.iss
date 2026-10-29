; Inno Setup 6 script for FlightRisk-Setup-<version>.exe, the Windows installer.
;
; Build on Windows with Inno Setup 6 (https://jrsoftware.org/isinfo.php):
;   iscc /DAppVersion=1.2.3 /DAppRef=v1.2.3 installer\windows\FlightRisk.iss
; The .exe is written to dist\.
;
; The Setup program is a small bootstrapper. It needs no administrator rights and
; copies nothing into Program Files: it runs install.ps1 (the same per-user
; installer as the command line, embedded in the .exe) in a visible PowerShell
; window, which shows the progress and installs FlightRisk into
; %LOCALAPPDATA%\FlightRisk. install.ps1 adds the Start menu entry and the
; Installed apps entry itself, so Setup registers no uninstaller of its own.
;
; Not built or run yet: this script was written on macOS. The .exe is not
; code-signed, so SmartScreen warns on first run (see docs/installer.md).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppRef
  #define AppRef ""
#endif

[Setup]
AppId={{6C2B7F1E-4D0B-4E3A-9B57-2F1D7A1C9E40}
AppName=FlightRisk
AppVersion={#AppVersion}
AppPublisher=Kurt Lehnardt
AppPublisherURL=https://github.com/KurtLehnardt/flightRisk
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CreateAppDir=no
Uninstallable=no
DisableProgramGroupPage=yes
DisableReadyPage=no
WizardStyle=modern
SetupIconFile=..\assets\FlightRisk.ico
OutputDir=..\..\dist
OutputBaseFilename=FlightRisk-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes

[Messages]
ReadyLabel1=Setup will now install FlightRisk for this Windows user.
ReadyLabel2a=A PowerShell window opens and shows the progress. It installs Python 3.13 and git with winget if they are missing, downloads FlightRisk and its Python packages (several GB) into %LOCALAPPDATA%\FlightRisk, and adds FlightRisk to the Start menu. Click Install to continue.

[Files]
Source: "install.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Run]
; FLIGHTRISK_REF pins the release this Setup was built for; an empty value lets the
; installer pick the newest release.
Filename: "{cmd}"; \
  Parameters: "/c set ""FLIGHTRISK_REF={#AppRef}"" & ""{sysnative}\WindowsPowerShell\v1.0\powershell.exe"" -NoProfile -ExecutionPolicy Bypass -File ""{tmp}\install.ps1"" || pause"; \
  StatusMsg: "Installing FlightRisk (see the PowerShell window)..."; \
  Flags: waituntilterminated
