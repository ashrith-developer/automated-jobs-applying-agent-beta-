@echo off
title JobFlow AI Agent Launcher
echo ===================================================
echo               JobFlow AI Agent Launcher
echo ===================================================
echo.

:: Clean up any stray background processes running on port 8000
echo [SYSTEM] Cleaning up any old server processes on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 2^>nul') do taskkill /f /pid %%a >nul 2>&1
echo.

:: Check if virtual environment exists
if not exist .venv (
    echo Creating virtual environment...
    py -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment using 'py'. Make sure Python is installed.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
    echo.
)

:: Activate virtual environment and install dependencies
echo Activating virtual environment...
call .venv\Scripts\activate

echo.
echo Installing/Updating dependencies from requirements.txt...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install pip requirements.
    pause
    exit /b 1
)

echo.
echo Installing Playwright Chromium browser...
playwright install chromium
if errorlevel 1 (
    echo [ERROR] Failed to install Playwright browser.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo [SUCCESS] JobFlow AI is fully configured!
echo Launching server at http://127.0.0.1:8000
echo ===================================================
echo.

:: Open the browser asynchronously after a 2 second delay
start /b cmd /c "timeout /t 3 >nul && start http://127.0.0.1:8000"

:: Start Uvicorn FastAPI server via Proactor loop launcher
python start_server.py
pause
