@echo off
setlocal
cd /d "%~dp0"
if not defined REDBOOK_RUNTIME_ROOT set "REDBOOK_RUNTIME_ROOT=E:\AI\codex\redbook_runtime"
set "KNOWLEDGE_DB_CREDENTIALS=%REDBOOK_RUNTIME_ROOT%\data\knowledge\postgresql-local\connection.json"
set "REDBOOK_AGENT_PORT=8786"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage_postgresql.ps1" -RuntimeRoot "%REDBOOK_RUNTIME_ROOT%" -Action start
if errorlevel 1 (
    echo Independent PostgreSQL failed to start. See %REDBOOK_RUNTIME_ROOT%\data\logs\postgresql\bootstrap.log
    exit /b 1
)
echo Editorial agent: http://127.0.0.1:%REDBOOK_AGENT_PORT%
"%~dp0.venv\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port %REDBOOK_AGENT_PORT%
endlocal
