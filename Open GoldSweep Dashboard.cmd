@echo off
rem Double-click to open the GoldSweep dashboard in your browser.
title GoldSweep dashboard
cd /d "%~dp0"
echo.
echo  Starting the GoldSweep dashboard. Your browser opens at http://localhost:8501
echo  in a few seconds. Keep this window open while you use it; closing it stops
echo  the dashboard only (the trading bot keeps running in its own window).
echo.
".venv\Scripts\python.exe" main.py --dashboard
echo.
echo  The dashboard has stopped. Press any key to close this window.
pause >nul
