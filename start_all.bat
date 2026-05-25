@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [1/2] Starting CaseFlow Server...
start "CaseFlow Server" cmd /k "cd /d %~dp0 && set PYTHONUTF8=1 && python start_server.py"
timeout /t 5 /nobreak >nul

echo [2/2] Starting Obsidian Bot...
start "Obsidian Bot" cmd /k "cd /d %~dp0 && set PYTHONUTF8=1 && python obsidian_agent.py"

echo Both started. Check the console windows for errors.
timeout /t 3 /nobreak >nul
