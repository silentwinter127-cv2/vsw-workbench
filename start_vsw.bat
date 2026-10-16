@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 横向视觉搜索工作台 Visual Search Workbench

set PORT=8799
set ROOT=%~dp0vsw\data

REM 手机端要连就必须监听局域网。加了 --lan 就绑 0.0.0.0，否则只绑本机。
REM 注意：绑 0.0.0.0 后同一 WiFi 下的设备都能访问，工作台目前没有口令。
set BIND=127.0.0.1
set LANNOTE=手机端：不开放（只监听本机）。想用手机版请运行 start_vsw_lan.bat 或加参数 --lan
if /i "%~1"=="--lan" (
  set BIND=0.0.0.0
  set LANNOTE=手机端：已开放（同一 WiFi 可访问，扫码见下方日志）
)

echo ============================================================
echo   横向视觉搜索工作台 (Visual Search Workbench)
echo ------------------------------------------------------------
echo   数据目录 : %ROOT%
echo   桌面地址 : http://127.0.0.1:%PORT%/
echo   手机地址 : http://^<本机局域网IP^>:%PORT%/m/
echo   语义引擎 : 本机 SigLIP2 (D:\siglip2-base-patch16-224)
echo ============================================================
echo.
echo %LANNOTE%
echo.
echo 说明：浏览器会自动打开。关掉这个窗口即停止服务。
echo       端口被占用时会自动往后找可用端口，请看窗口里的实际地址。
echo.

python -m vsw.server --root "%ROOT%" --host %BIND% --port %PORT% --allow-private

if errorlevel 1 (
  echo.
  echo [错误] 启动失败。请确认已安装 Python 3.10+ 且可在命令行运行 python。
  pause
)
endlocal
