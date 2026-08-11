@echo off
rem Restart SiverWXbot panel inside the interactive session.
rem SAFETY: only kills python.exe whose command line contains web_server.py.
rem NEVER kill by port - WeChat listens on 1000x ports locally too.
rem wxautox4 41.x logs the login nickname, which contains emoji.
rem Default GBK stdout raises UnicodeEncodeError and kills bot init.
set PYTHONIOENCODING=utf-8
cd /d C:\Users\Admin\SiverWXbot_plus-main
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'web_server' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
timeout /t 3 /nobreak >nul
start "SWXPanel" /min cmd /c "python web_server.py >> panel_logs\panel_restart.log 2>&1"
