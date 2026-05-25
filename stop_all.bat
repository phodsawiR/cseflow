@echo off
echo Stopping CaseFlow...
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq CaseFlow Server" /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq Obsidian Bot" /F >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%obsidian_agent%%'" delete >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%start_server%%'" delete >nul 2>&1
del /f /q "%~dp0.start_server.pid" >nul 2>&1
del /f /q "%~dp0.obsidian_agent.pid" >nul 2>&1
echo Done.
timeout /t 2 /nobreak >nul
