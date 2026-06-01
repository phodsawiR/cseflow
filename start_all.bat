@echo off
cd /d "%~dp0"

echo [0/2] Killing existing processes...
powershell -NoProfile -Command "Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -match 'obsidian_agent|start_server|vault_builder' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA 0 }"
del /f /q "%~dp0.obsidian_agent.pid" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [1/2] Starting CaseFlow Server...
start "CaseFlow Server" cmd /k "cd /d %~dp0 && .venv\Scripts\python.exe -X utf8 start_server.py"
timeout /t 5 /nobreak >nul

echo [2/2] Starting Obsidian Bot...
start "Obsidian Bot" cmd /k "cd /d %~dp0 && .venv\Scripts\python.exe -X utf8 obsidian_agent.py"

echo Both started.
timeout /t 3 /nobreak >nul
