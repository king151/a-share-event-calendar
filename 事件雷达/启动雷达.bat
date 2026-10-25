@echo off
chcp 65001 >nul
cd /d %~dp0
rem 用端口 7610 判断是否已在运行（比 tasklist 更准：桌宠等其他 pythonw 进程不会误判）
netstat -ano | findstr ":7610" >nul 2>nul
if errorlevel 1 (
    start "event-radar" /min "D:\Python310\pythonw.exe" radar_server.py
    echo 事件雷达已启动（127.0.0.1:7610，后台运行）
) else (
    echo 事件雷达已在运行（127.0.0.1:7610）
)
timeout /t 2 >nul