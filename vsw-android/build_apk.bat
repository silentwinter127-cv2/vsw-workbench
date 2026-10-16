@echo off
REM ===========================================================================
REM  Visual Search Workbench - Android build (NO Gradle / NO Android Studio)
REM
REM  aapt2 -> javac -> d8 -> zip -> zipalign -> apksigner
REM
REM  Usage:
REM    build_apk.bat            debug build (self-signed, installable)
REM    build_apk.bat release    release build (signed with keystore)
REM
REM  NOTE: this file must stay ASCII-only. cmd.exe parses .bat in the OEM code
REM  page; a UTF-8 Chinese comment here breaks the parser (learned the hard way).
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set BUILD_ROOT=C:\vsw-build
set SDK=%BUILD_ROOT%\android-sdk
set BT=%SDK%\build-tools\34.0.0
set PLATFORM=%SDK%\platforms\android-34\android.jar
set ZXING=%BUILD_ROOT%\libs\zxing-core-3.5.3.jar

if not exist "%BUILD_ROOT%\java-home.txt" (
  echo [ERROR] missing %BUILD_ROOT%\java-home.txt
  exit /b 1
)
set /p JAVA_HOME=<"%BUILD_ROOT%\java-home.txt"
set PATH=%JAVA_HOME%\bin;%PATH%

if not exist "%BT%\aapt2.exe"     ( echo [ERROR] missing aapt2 & exit /b 1 )
if not exist "%BT%\d8.bat"        ( echo [ERROR] missing d8 & exit /b 1 )
if not exist "%BT%\zipalign.exe"  ( echo [ERROR] missing zipalign & exit /b 1 )
if not exist "%BT%\apksigner.bat" ( echo [ERROR] missing apksigner & exit /b 1 )
if not exist "%PLATFORM%"         ( echo [ERROR] missing android.jar & exit /b 1 )
if not exist "%ZXING%"            ( echo [ERROR] missing zxing jar & exit /b 1 )

set MODE=%1
if "%MODE%"=="" set MODE=debug

set OUT=build
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%" 2>nul
mkdir "%OUT%\classes" 2>nul
mkdir "%OUT%\dex" 2>nul
mkdir "%OUT%\assets\mobile" 2>nul
mkdir "%OUT%\gen" 2>nul

echo === 1/8 sync web app from ..\vsw\static\mobile ===
copy /y "..\vsw\static\mobile\index.html" "%OUT%\assets\mobile\index.html" >nul
if errorlevel 1 ( echo [ERROR] copy mobile page failed & exit /b 1 )

echo === 2/8 aapt2 compile resources ===
"%BT%\aapt2.exe" compile --dir res -o "%OUT%\res.zip" > "%OUT%\aapt2-compile.log" 2>&1
if errorlevel 1 (
  echo [ERROR] aapt2 compile failed:
  type "%OUT%\aapt2-compile.log"
  exit /b 1
)

echo === 3/8 aapt2 link ===
REM -R res.zip with --auto-add-overlay: our resources are all "new" relative to the
REM android.jar, and without that flag aapt2 rejects them in overlay mode.
REM -0 .html keeps the built-in page STORED (uncompressed). It is small, and
REM compressed assets are the thing that made the WebView report ERR_FILE_NOT_FOUND
REM on a real device; belt and braces on top of copying it to private storage.
"%BT%\aapt2.exe" link -o "%OUT%\base.apk" -I "%PLATFORM%" --manifest AndroidManifest.xml -R "%OUT%\res.zip" --auto-add-overlay --java "%OUT%\gen" -A "%OUT%\assets" -0 .html --min-sdk-version 24 --target-sdk-version 34 --version-code 1 --version-name 1.0 > "%OUT%\aapt2-link.log" 2>&1
if errorlevel 1 (
  echo [ERROR] aapt2 link failed:
  type "%OUT%\aapt2-link.log"
  exit /b 1
)

echo === 4/8 javac ===
dir /s /b java\*.java "%OUT%\gen\*.java" > "%OUT%\sources.txt"
"%JAVA_HOME%\bin\javac.exe" -encoding UTF-8 -source 8 -target 8 -nowarn -bootclasspath "%PLATFORM%" -classpath "%PLATFORM%;%ZXING%" -d "%OUT%\classes" "@%OUT%\sources.txt" > "%OUT%\javac.log" 2>&1
if errorlevel 1 (
  echo [ERROR] javac failed:
  type "%OUT%\javac.log"
  exit /b 1
)

echo === 5/8 unpack zxing classes ===
pushd "%OUT%\classes"
"%JAVA_HOME%\bin\jar.exe" xf "%ZXING%"
popd
if errorlevel 1 ( echo [ERROR] unpack zxing failed & exit /b 1 )

echo === 6/8 d8 ===
dir /s /b "%OUT%\classes\*.class" > "%OUT%\classes.txt"
REM Call d8's jar directly instead of d8.bat: invoking a .bat that does its own
REM argument juggling from inside another .bat silently terminated this script
REM right after this step (exit code 0, no artifact). The jar call is stable.
"%JAVA_HOME%\bin\java.exe" -Xmx1024m -cp "%BT%\lib\d8.jar" com.android.tools.r8.D8 --lib "%PLATFORM%" --min-api 24 --output "%OUT%\dex" "@%OUT%\classes.txt" > "%OUT%\d8.log" 2>&1
if errorlevel 1 (
  echo [ERROR] d8 failed:
  type "%OUT%\d8.log"
  exit /b 1
)

echo === 7/8 pack dex into apk ===
pushd "%OUT%\dex"
"%JAVA_HOME%\bin\jar.exe" uf "..\base.apk" classes.dex
popd
if errorlevel 1 ( echo [ERROR] pack dex failed & exit /b 1 )

echo === 8/8 align + sign (%MODE%) ===
copy /y "%OUT%\base.apk" "%OUT%\vsw-unsigned.apk" >nul
"%BT%\zipalign.exe" -f -p 4 "%OUT%\vsw-unsigned.apk" "%OUT%\vsw-aligned.apk"
if errorlevel 1 ( echo [ERROR] zipalign failed & exit /b 1 )

set KS=%BUILD_ROOT%\vsw-release.keystore
REM CHANGE THIS before your first release build. The default below is a placeholder
REM that is published in the repository, so a keystore created with it is worthless
REM as an identity: anyone could sign an "update" for your app. Pick your own strong
REM passphrase, keep it out of version control, and back up both the keystore and the
REM passphrase -- losing them means installed users can never upgrade (Android only
REM accepts an update signed by the same key).
if not defined KS_PASS set KS_PASS=CHANGE-ME-before-releasing

set SIGNER=%BT%\lib\apksigner.jar
set SIGNER_CMD="%JAVA_HOME%\bin\java.exe" -Xmx512m -cp "%SIGNER%" com.android.apksigner.ApkSignerTool

if "%MODE%"=="release" (
  if not exist "%KS%" (
    echo    generating release keystore ...
    "%JAVA_HOME%\bin\keytool.exe" -genkeypair -v -keystore "%KS%" -alias vsw -keyalg RSA -keysize 2048 -validity 10950 -storepass %KS_PASS% -keypass %KS_PASS% -dname "CN=Visual Search Workbench, OU=vsw, O=vsw, C=CN"
    if errorlevel 1 ( echo [ERROR] keytool failed & exit /b 1 )
  )
  set FINAL=..\vsw-workbench.apk
  %SIGNER_CMD% sign --ks "%KS%" --ks-key-alias vsw --ks-pass pass:%KS_PASS% --key-pass pass:%KS_PASS% --v1-signing-enabled true --v2-signing-enabled true --out "!FINAL!" "%OUT%\vsw-aligned.apk"
) else (
  set FINAL=..\vsw-workbench-debug.apk
  %SIGNER_CMD% sign --ks "%BUILD_ROOT%\debug.keystore" --ks-key-alias vswdebug --ks-pass pass:android --key-pass pass:android --v1-signing-enabled true --v2-signing-enabled true --out "!FINAL!" "%OUT%\vsw-aligned.apk"
)
if errorlevel 1 ( echo [ERROR] apksigner failed & exit /b 1 )

echo.
echo === verify ===
%SIGNER_CMD% verify --print-certs "!FINAL!"
"%BT%\aapt.exe" dump badging "!FINAL!" > "%OUT%\badging.txt"
findstr /b "package application-label launchable-activity sdkVersion targetSdkVersion uses-permission" "%OUT%\badging.txt"
echo.
for %%F in ("!FINAL!") do echo OUTPUT: %%~fF  (%%~zF bytes)
endlocal
