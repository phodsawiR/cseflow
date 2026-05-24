@echo off
echo Stopping CaseFlow...
taskkill /FI "WINDOWTITLE eq CaseFlow Server" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Obsidian Bot" /F >nul 2>&1
del /f /q "%~dp0.start_server.pid" >nul 2>&1
del /f /q "%~dp0.obsidian_agent.pid" >nul 2>&1
echo Done.
timeout /t 2 /nobreak >nul
