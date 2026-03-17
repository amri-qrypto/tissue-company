@echo off
title Sharky Strategy Bot
echo.
echo ========================================
echo   Sharky Strategy Bot - Polymarket
echo ========================================
echo.

REM Check if .env exists
if not exist ".env" (
    echo [!] No .env file found.
    echo [!] Copy .env.example to .env and fill in your wallet details.
    echo.
    echo     copy .env.example .env
    echo.
    pause
    exit /b 1
)

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found. Please install Python 3.9+.
    pause
    exit /b 1
)

REM Install dependencies if needed
echo [*] Checking dependencies...
pip install py-clob-client requests python-dotenv --quiet 2>nul

echo.
echo [*] Starting bot...
echo [*] Press Ctrl+C to stop.
echo.

python sharky_bot.py run

pause
