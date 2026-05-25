@echo off
cd /d "%~dp0"
echo Stopping CaseFlow...

powershell -NoProfile -Command "Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -match 'obsidian_agent|start_server|vault_builder' } | ForEach-Object { Write-Host 'Killing PID' $_.ProcessId; Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }"

del /f /q "%~dp0.start_server.pid" >nul 2>&1
del /f /q "%~dp0.obsidian_agent.pid" >nul 2>&1

echo Done. Waiting 5s...
timeout /t 5 /nobreak >nul
