@echo off
rem ---------------------------------------------------------------
rem  Stop the watchdog and unregister its scheduled tasks.
rem  The workbench server itself is left running (kill it yourself
rem  if you really want it down).
rem
rem  Usage: uninstall_watchdog.bat
rem  Keep this file pure ASCII.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "TASK=VSW Workbench Watchdog"

echo [1/3] Unregister tasks
schtasks /End /TN "%TASK%" >nul 2>nul
schtasks /End /TN "%TASK% (boot)" >nul 2>nul
schtasks /Delete /TN "%TASK%" /F >nul 2>nul
schtasks /Delete /TN "%TASK% (boot)" /F >nul 2>nul

echo [2/3] Stop watchdog processes
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*vsw-watchdog*' } | ForEach-Object { Write-Host ('    killing PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [3/3] Remove lock + task leftovers
del /q "%~dp0vsw\data\logs\watchdog.lock" >nul 2>nul

echo.
echo Done. The watchdog will not come back on next logon.
echo (Logs are kept in vsw\data\logs\ )
exit /b 0
