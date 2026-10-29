@echo off
rem Open the FlightRisk launcher window (Windows: double-click this file in Explorer).
rem
rem It runs the launcher with the virtual environment in this repository, serves the window on
rem http://127.0.0.1:5500/ and opens it in the browser. The edge token is read by the launcher
rem itself from FLIGHTRISK_EDGE_TOKEN or %USERPROFILE%\.flightrisk-fleet.env. Extra arguments are
rem passed on, for example --port 5501. The child processes get no console windows of their own;
rem their output is in the launcher window and in logs\launcher\. See docs\launcher.md.
setlocal
cd /d "%~dp0.."
if not exist "venv\Scripts\python.exe" (
    echo launcher.bat: venv\Scripts\python.exe was not found. Create the virtual environment first ^(README, Quick Start^).
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m flightrisk.launcher %*
if errorlevel 1 pause
endlocal
