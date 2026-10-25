@echo off
rem ---------------------------------------------------------------
rem  The launcher that Task Scheduler calls.
rem  Runs the watchdog with pythonw so no console window flashes up.
rem  Keep this file pure ASCII: cmd.exe parses .bat byte by byte.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "HERE=%~dp0"
set "WDLOG=%~dp0vsw\data\logs"

rem  Prefer pythonw (no window); fall back to python if it is missing.
set "PYW="
for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" set "PYW=%%~$PATH:P"
set "PY="
for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PY=%%~$PATH:P"
if defined PYW goto havepy
if defined PY goto havepy
echo [vsw] python not found on PATH. Install Python 3.10+ first.
exit /b 2
:havepy
if not defined PYW set "PYW=%PY%"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PYW%" "%HERE%scripts\vsw-watchdog.py" --root "%HERE%vsw\data" --host 0.0.0.0 --port 8799 --gh silentwinter127-cv2/vsw-workbench %*
exit /b %errorlevel%
