@echo off
cd /d "%~dp0"

echo [1/2] Starting CaseFlow Server...
start "CaseFlow Server" cmd /k "cd /d %~dp0 && python -X utf8 start_server.py"
timeout /t 5 /nobreak >nul

echo [2/2] Starting Obsidian Bot...
start "Obsidian Bot" cmd /k "cd /d %~dp0 && python -X utf8 obsidian_agent.py"

echo Both started.
timeout /t 3 /nobreak >nul
