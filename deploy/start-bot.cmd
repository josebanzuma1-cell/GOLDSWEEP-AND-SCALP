@echo off
rem Starts the scheduled GoldSweep bot now (and lets Task Scheduler keep it running).
schtasks /Change /TN "GoldSweep - Bot" /ENABLE
schtasks /Run /TN "GoldSweep - Bot"
echo GoldSweep bot started in the background. For LIVE, send /golive ^<account^> on Telegram.
pause
