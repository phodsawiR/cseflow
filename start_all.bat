@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo Starting CaseFlow...
start "CaseFlow Server" python start_server.py
timeout /t 3 /nobreak >nul
start "Obsidian Bot" python obsidian_agent.py
echo Both services started. You can close this window.
timeout /t 5 /nobreak >nul
