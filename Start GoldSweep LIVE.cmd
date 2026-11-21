@echo off
rem Double-click to start GoldSweep live trading (needs MODE=LIVE in .env and mode: LIVE in config.yaml).
title GoldSweep LIVE
cd /d "%~dp0"
echo.
echo  Starting GoldSweep LIVE. Loading and connecting to MetaTrader 5 can take
echo  up to a minute with nothing on screen: please wait.
echo  Do not click inside this window (that pauses it; press Esc if the title says Select).
echo.
".venv\Scripts\python.exe" main.py --live
echo.
echo  GoldSweep has stopped. Open positions keep their stop-loss and take-profit at the broker.
echo  Press any key to close this window.
pause >nul
