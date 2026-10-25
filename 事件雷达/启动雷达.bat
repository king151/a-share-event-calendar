@echo off
chcp 65001 >nul
cd /d %~dp0
rem 用端口 7610 判断是否已在运行（比 tasklist 更准：桌宠等其他 pythonw 进程不会误判）
netstat -ano | findstr ":7610" >nul 2>nul
if errorlevel 1 (
    start "event-radar" /min "D:\Python310\pythonw.exe" radar_server.py
    echo 事件雷达已启动（127.0.0.1:7610，后台运行）
    rem 启动自检：3 秒后探测 /api/ping，失败请查 radar.log（异常均会留痕）
    timeout /t 3 >nul
    curl -s -o nul http://127.0.0.1:7610/api/ping
    if errorlevel 1 (
        echo [警告] 自检未通过：服务未响应。请查看本目录 radar.log 排查。
    ) else (
        echo 自检通过：服务正常响应。
    )
) else (
    echo 事件雷达已在运行（127.0.0.1:7610）
)
timeout /t 2 >nul