@echo off
rem Stops the scheduled GoldSweep bot and keeps it stopped (until start-bot.cmd).
rem Open positions keep their stop-loss and take-profit at the broker.
schtasks /Change /TN "GoldSweep - Bot" /DISABLE
schtasks /End /TN "GoldSweep - Bot"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'main.py --(live|paper)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo GoldSweep bot stopped. Double-click start-bot.cmd to start it again.
pause
