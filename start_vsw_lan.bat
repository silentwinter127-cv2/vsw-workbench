@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 以「局域网模式」启动：手机连同一个 WiFi 就能用手机版。
echo 安全提醒：同一网络下的设备都能访问这个工作台（目前没有口令）。在公共 WiFi 上别这么开。
echo.
call "%~dp0start_vsw.bat" --lan
