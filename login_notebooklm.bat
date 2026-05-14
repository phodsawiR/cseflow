@echo off
echo =======================================================
echo   NotebookLM Re-Authentication (Direct Mode)
echo =======================================================
echo Checking environment...
if not exist ".\.venv\Scripts\notebooklm.exe" (
    echo [ERROR] .\.venv\Scripts\notebooklm.exe not found!
    pause
    exit /b
)

echo Opening browser for Google login...
echo Please log in and then return to this window.
echo.

".\.venv\Scripts\notebooklm.exe" login

echo.
echo =======================================================
echo If you saw "Login successful", you can close this.
echo =======================================================
pause
