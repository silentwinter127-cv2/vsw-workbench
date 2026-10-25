@echo off
rem ---------------------------------------------------------------
rem  Run the watchdog in the foreground (console stays open, log lines
rem  stream to the window). Handy when you want to watch it work.
rem  For "start with Windows" use install_watchdog.bat instead.
rem
rem  Usage: start_watchdog.bat [extra args for vsw-watchdog.py]
rem  Keep this file pure ASCII.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
echo Starting the watchdog in this window (Ctrl+C to stop)...
echo Log file: %~dp0vsw\data\logs\watchdog.log
echo.
python "%~dp0scripts\vsw-watchdog.py" --root "%~dp0vsw\data" --host 0.0.0.0 --port 8799 --gh silentwinter127-cv2/vsw-workbench %*
exit /b %errorlevel%
