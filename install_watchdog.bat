@echo off
rem ---------------------------------------------------------------
rem  Register the watchdog so it starts with Windows and keeps the
rem  workbench server alive (restarts it whenever it dies).
rem
rem  Usage:
rem    install_watchdog.bat            -> at logon, current user
rem    install_watchdog.bat system     -> at boot, as SYSTEM (no login needed)
rem
rem  Keep this file pure ASCII.
rem ---------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "TASK=VSW Workbench Watchdog"
set "TARGET=%~dp0watchdog_run.bat"
set "MODE=%~1"

if /i "%MODE%"=="system" goto system
if /i "%MODE%"=="user" goto user
if "%MODE%"=="" goto user
echo Usage: install_watchdog.bat [user^|system]
exit /b 2

:user
echo [1/2] Register task "%TASK%" (at logon, current user, highest privileges)
schtasks /Create /TN "%TASK%" /TR "\"%TARGET%\"" /SC ONLOGON /RL HIGHEST /F
if errorlevel 1 (
  echo     ... highest privileges refused, retrying without /RL
  schtasks /Create /TN "%TASK%" /TR "\"%TARGET%\"" /SC ONLOGON /F
)
if errorlevel 1 goto failed
goto verify

:system
echo [1/2] Register task "%TASK% (boot)" (at startup, as SYSTEM)
schtasks /Create /TN "%TASK% (boot)" /TR "\"%TARGET%\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto failed
goto verify

:verify
echo [2/2] Now registered:
schtasks /Query /TN "%TASK%" /FO LIST 2>nul | findstr /R /C:"TaskName" /C:"Status" /C:"Next Run"
schtasks /Query /TN "%TASK% (boot)" /FO LIST 2>nul | findstr /R /C:"TaskName" /C:"Status"
echo.
echo Done. Start it right now with:
echo     schtasks /Run /TN "%TASK%"
echo Logs:    vsw\data\logs\watchdog.log  (and server.log)
echo Status:  vsw\data\logs\watchdog-status.json
echo Remove:  uninstall_watchdog.bat
exit /b 0

:failed
echo.
echo Registering the task failed. Run this file as Administrator
echo (right click -> "Run as administrator"), then try again.
exit /b 1
