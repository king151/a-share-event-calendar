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
rem Use port 7610 to check whether it is already running (more accurate than tasklist: other pythonw processes such as desktop pets will not be misjudged)
netstat -ano | findstr ":7610" >nul 2>nul
if errorlevel 1 (
    start "event-radar" /min "%PY%" radar_server.py
    echo Event radar started (127.0.0.1:7610, running in background; requires pip install akshare)
    rem Start self-check: after 3 seconds probe /api/ping; if it fails, check radar.log (all exceptions are logged)
    timeout /t 3 >nul
    curl -s -o nul http://127.0.0.1:7610/api/ping
    if errorlevel 1 (
        echo [Warning] Self-check failed: the service is not responding, which is most likely due to missing the akshare dependency; run pip install akshare then double-click this script again, or check radar.log in this directory for details.
    ) else (
        echo Self-check passed: the service responded normally.
    )
) else (
    echo Event radar is already running (127.0.0.1:7610)
)
timeout /t 2 >nul
