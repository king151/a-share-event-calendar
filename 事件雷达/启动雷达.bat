@echo off
chcp 65001 >nul
cd /d %~dp0
rem Python 解释器解析：环境变量 RADAR_PYTHON 可覆盖；否则本机熟路 D:\Python310，再退回 PATH 里的 pythonw/python
set "PY=%RADAR_PYTHON%"
if not defined PY if exist "D:\Python310\pythonw.exe" set "PY=D:\Python310\pythonw.exe"
if not defined PY for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  echo [Error] No Python interpreter found: please install Python 3.10+ and ensure python.exe is in your PATH (or set the environment variable RADAR_PYTHON to point to the full path of pythonw.exe).
  pause
  exit /b 1
)
rem 用端口 7610 的 LISTENING 态判断是否已在运行（TIME_WAIT 不算）
netstat -ano | findstr ":7610.*LISTENING" >nul 2>nul
if errorlevel 1 (
    start "event-radar" /min "%PY%" radar_server.py
    rem ping 式延时：无控制台环境 timeout 会报 Input redirection 错
    ping -n 4 127.0.0.1 >nul
    echo 事件雷达已启动（127.0.0.1:7610，后台运行；需 pip install akshare）
    curl -s -o nul http://127.0.0.1:7610/api/ping
    if errorlevel 1 (
        echo [警告] 自检未通过：多半是 akshare 依赖未装（pip install akshare），或启动较慢——稍等几秒刷新页面重试，详见本目录 radar.log
    ) else (
        echo 自检通过：服务正常响应。
    )
) else (
    echo 事件雷达已在运行（127.0.0.1:7610）
)
ping -n 3 127.0.0.1 >nul
