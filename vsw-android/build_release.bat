@echo off
REM ===========================================================================
REM  Release build helper.
REM
REM  Why this exists: the signing passphrase must NOT live in the repository.
REM  build_apk.bat reads KS_PASS from the environment, so this script pulls it from
REM  the passphrase file outside the repo (C:\vsw-build) and then calls the build.
REM
REM  Usage:  build_release.bat
REM ===========================================================================
setlocal
cd /d "%~dp0"

set KS_FILE=%VSW_BUILD_ROOT%\KEYSTORE-passphrase.txt
if not defined VSW_BUILD_ROOT set KS_FILE=C:\vsw-build\KEYSTORE-passphrase.txt

if not exist "%KS_FILE%" (
  echo [ERROR] passphrase file not found: %KS_FILE%
  echo         Create it with a single line:  KS_PASS=your-strong-passphrase
  echo         Keep it OUTSIDE the repository and back it up.
  exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in ("%KS_FILE%") do (
  if /i "%%A"=="KS_PASS" set KS_PASS=%%B
)
if not defined KS_PASS (
  echo [ERROR] %KS_FILE% has no KS_PASS= line
  exit /b 1
)

call "%~dp0build_apk.bat" release
endlocal
