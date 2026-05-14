@echo off
echo =======================================================
echo   CaseFlow API Server (Local Mode)
echo =======================================================
echo   Local URL : http://127.0.0.1:8000/static/index.html
echo   API Docs  : http://127.0.0.1:8000/docs
echo =======================================================
echo Press Ctrl+C to stop the server
echo.

.\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
pause
