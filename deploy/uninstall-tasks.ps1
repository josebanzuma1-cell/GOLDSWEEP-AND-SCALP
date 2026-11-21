<#
.SYNOPSIS
    Removes the GoldSweep tasks from Task Scheduler and stops a running scheduled bot.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\uninstall-tasks.ps1
#>
$names = @(
    "GoldSweep - Bot",
    "GoldSweep - Watchdog",
    "GoldSweep - Dashboard",
    "GoldSweep - MetaTrader 5"
)
foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) { continue }
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    "Removed: $name"
}
"Open positions keep their stop-loss and take-profit at the broker."
