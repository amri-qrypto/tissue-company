@echo off
echo ==========================================
echo   Polymarket Dashboard - Starting Server
echo ==========================================
echo.
echo Starting local server on http://localhost:8000
echo Opening dashboard in your browser...
echo.
echo (Keep this window open while using the dashboard)
echo (Press Ctrl+C to stop)
echo.
start http://localhost:8000/dashboard.html
python -m http.server 8000
pause
