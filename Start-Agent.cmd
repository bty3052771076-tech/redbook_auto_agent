@echo off
setlocal
cd /d "%~dp0"
set "REDBOOK_RUNTIME_ROOT=E:\AI\codex\redbook_runtime"
set "KNOWLEDGE_DB_CREDENTIALS=%REDBOOK_RUNTIME_ROOT%\data\knowledge\postgresql-local\connection.json"
set "REDBOOK_AGENT_PORT=8786"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REDBOOK_RUNTIME_ROOT%\data\runtime\postgresql\manage.ps1" -Action start
if errorlevel 1 (
    echo Independent PostgreSQL failed to start. See E:\AI\codex\redbook_runtime\data\logs\postgresql\bootstrap.log
    exit /b 1
)
echo Editorial agent: http://127.0.0.1:%REDBOOK_AGENT_PORT%
"%~dp0.venv\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port %REDBOOK_AGENT_PORT%
endlocal
