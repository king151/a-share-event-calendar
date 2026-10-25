@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
rem Python 解释器解析：环境变量 RADAR_PYTHON 可覆盖；再退回 PATH 里的 pythonw/python
set "PY="
if defined RADAR_PYTHON set "PY=%RADAR_PYTHON%"
if not defined PY if exist "D:\Python310\pythonw.exe" set "PY=D:\Python310\pythonw.exe"
if not defined PY call :findpy pythonw.exe
if not defined PY call :findpy python.exe
if not defined PY goto nopy

netstat -ano | findstr ":7610" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 goto startsrv
echo 事件雷达已在运行（127.0.0.1:7610）
ping -n 3 127.0.0.1 >nul
exit /b 0

:startsrv
start "event-radar" /min "%PY%" radar_server.py
ping -n 4 127.0.0.1 >nul
echo 事件雷达已启动（127.0.0.1:7610，后台运行；需 pip install akshare）
curl -s -o nul http://127.0.0.1:7610/api/ping
if errorlevel 1 goto selffail
echo 自检通过：服务正常响应。
exit /b 0

:selffail
echo [警告] 自检未通过：多半是 akshare 依赖未装（pip install akshare），或启动较慢，稍等几秒重试；详见本目录 radar.log
exit /b 0

:nopy
echo [Error] 未找到 Python 解释器：请安装 Python 3.10+ 并确保 python.exe 在 PATH 里，或设置环境变量 RADAR_PYTHON 指向 pythonw.exe 全路径
exit /b 1

:findpy
for /f "delims=" %%i in ('where %1 2^>nul') do (
    set "PY=%%i"
    goto :eof
)
goto :eof
