@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo ERROR: .env is missing. Copy .env.example to .env and fill in local settings.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: The project's Python .venv is missing. Create it and install requirements.txt.
    pause
    exit /b 1
)
if not exist "frontend\node_modules\vite\bin\vite.js" (
    echo ERROR: Frontend packages are missing. Open a terminal in frontend and run npm install.
    pause
    exit /b 1
)

start "RE.Pro Dashboard API" /D "%~dp0" cmd /k ".venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"
start "RE.Pro DSM Dashboard" /D "%~dp0frontend" cmd /k "npm.cmd run dev"
echo Started the API and dashboard in separate terminal windows.
echo Open http://127.0.0.1:5173 after the frontend window reports it is ready.
endlocal
