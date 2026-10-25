@echo off
rem ---------------------------------------------------------------
rem  Register the watchdog so it starts with Windows and keeps the
rem  workbench server alive (restarts it whenever it dies).
rem
rem  Usage:
rem    install_watchdog.bat            -> at logon, current user
rem    install_watchdog.bat system     -> at boot, as SYSTEM (no login needed)
rem    install_watchdog.bat remove     -> unregister
rem
rem  The real work happens in scripts\vsw-install-task.ps1, because the task
rem  must launch pythonw.exe DIRECTLY: going through a .bat means cmd.exe (a
rem  console program) starts on every trigger, which is what produced the
rem  "black window keeps flashing" report.
rem
rem  Keep this file pure ASCII.
rem ---------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MODE=%~1"
if /i "%MODE%"=="" set "MODE=user"
if /i "%MODE%"=="user" goto run
if /i "%MODE%"=="system" goto run
if /i "%MODE%"=="remove" set "EXTRA=-Remove" & goto run
echo Usage: install_watchdog.bat [user^|system^|remove]
exit /b 2

:run
set "PS1=%~dp0scripts\vsw-install-task.ps1"
if not exist "%PS1%" (
  echo [vsw] missing %PS1%
  exit /b 1
)
echo [1/2] Registering scheduled task (mode: %MODE%)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode %MODE% %EXTRA%
if errorlevel 1 goto failed

echo [2/2] Now:
schtasks /Query /TN "VSW Workbench Watchdog" /FO LIST 2>nul | findstr /R /C:"TaskName" /C:"Status" /C:"Next Run"
schtasks /Query /TN "VSW Workbench Watchdog (boot)" /FO LIST 2>nul | findstr /R /C:"TaskName" /C:"Status"
echo.
echo Done. Start it right now with:
echo     schtasks /Run /TN "VSW Workbench Watchdog"
echo Logs:    vsw\data\logs\watchdog.log  (and server.log)
echo Status:  vsw\data\logs\watchdog-status.json
echo Remove:  uninstall_watchdog.bat
exit /b 0

:failed
echo.
echo Registering the task failed. Run this file as Administrator
echo (right click -^> "Run as administrator"), then try again.
exit /b 1
