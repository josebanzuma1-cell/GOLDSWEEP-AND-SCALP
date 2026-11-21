<#
.SYNOPSIS
    Creates the Task Scheduler tasks that keep GoldSweep running (spec 9.8).

.DESCRIPTION
    For the Windows user that runs this script, after every logon:
      GoldSweep - MetaTrader 5  starts the terminal (it remembers its login).
      GoldSweep - Bot           starts the bot 90 s later, without a console window,
                                and checks again every 5 minutes: if the bot has
                                stopped for any reason, it is started again.
                                LIVE runs "--live --unattended": it trades only after
                                you send /golive <account> on Telegram.
      GoldSweep - Watchdog      every 15 minutes: a Telegram alert if the bot has gone
                                quiet while the gold market is open.
      GoldSweep - Dashboard     (only with -Dashboard) opens the dashboard.

    The tasks run in your own session (MetaTrader 5 needs a desktop), so the user
    must be logged on; on a VPS, set up automatic logon (docs/DEPLOYMENT.md).
    Run it again after moving the project; it replaces the tasks.

.PARAMETER Paper
    Paper-trade instead of live (--paper).
.PARAMETER Dashboard
    Also open the dashboard after logon.
.PARAMETER Mt5Path
    terminal64.exe of the MetaTrader 5 terminal GoldSweep uses.
.PARAMETER DryRun
    Build and show the tasks without creating anything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 -Paper -DryRun
#>
param(
    [switch]$Paper,
    [switch]$Dashboard,
    [string]$Mt5Path = "C:\Program Files\MetaTrader 5\terminal64.exe",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "The Python environment was not found at $pythonw. Follow the setup in README.md first."
}
if (-not (Test-Path $Mt5Path)) {
    throw "MetaTrader 5 was not found at $Mt5Path. Pass -Mt5Path with the right terminal64.exe."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

function New-GoldSweepSettings {
    # No time limit, never two copies at once, runs on battery, catches up after sleep.
    New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
}

function New-LogonTrigger([int]$DelaySeconds, [int]$EveryMinutes) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = "PT${DelaySeconds}S"
    if ($EveryMinutes -gt 0) {
        # Repeat for as long as the session lasts (no duration = indefinitely).
        $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
        $trigger.Repetition = $repeat.Repetition
    }
    $trigger
}

$botArguments = if ($Paper) { "main.py --paper" } else { "main.py --live --unattended" }
$tasks = @(
    @{
        Name        = "GoldSweep - MetaTrader 5"
        Description = "Starts the MetaTrader 5 terminal that GoldSweep trades through."
        Action      = New-ScheduledTaskAction -Execute $Mt5Path
        Trigger     = New-LogonTrigger -DelaySeconds 10 -EveryMinutes 0
    },
    @{
        Name        = "GoldSweep - Bot"
        Description = "Runs GoldSweep ($botArguments); started again within 5 minutes if it stops."
        Action      = New-ScheduledTaskAction -Execute $pythonw -Argument $botArguments -WorkingDirectory $project
        Trigger     = New-LogonTrigger -DelaySeconds 90 -EveryMinutes 5
    },
    @{
        Name        = "GoldSweep - Watchdog"
        Description = "Telegram alert when GoldSweep has gone quiet while the gold market is open."
        Action      = New-ScheduledTaskAction -Execute $pythonw -Argument "main.py --watchdog" -WorkingDirectory $project
        Trigger     = New-LogonTrigger -DelaySeconds 600 -EveryMinutes 15
    }
)
if ($Dashboard) {
    $tasks += @{
        Name        = "GoldSweep - Dashboard"
        Description = "Opens the GoldSweep dashboard at http://localhost:8501."
        Action      = New-ScheduledTaskAction -Execute $pythonw -Argument "main.py --dashboard" -WorkingDirectory $project
        Trigger     = New-LogonTrigger -DelaySeconds 150 -EveryMinutes 0
    }
}

foreach ($task in $tasks) {
    $definition = New-ScheduledTask -Action $task.Action -Trigger $task.Trigger `
        -Principal $principal -Settings (New-GoldSweepSettings) -Description $task.Description
    if ($DryRun) {
        $every = $task.Trigger.Repetition.Interval
        "[dry run] {0}: {1} {2} (at logon after {3}{4})" -f $task.Name, $task.Action.Execute,
            $task.Action.Arguments, $task.Trigger.Delay, $(if ($every) { ", then every $every" } else { "" })
    } else {
        Register-ScheduledTask -TaskName $task.Name -InputObject $definition -Force | Out-Null
        "Created: $($task.Name)"
    }
}
if (-not $DryRun) {
    ""
    "Done. The tasks start at your next logon. To start them now:"
    "  Start-ScheduledTask -TaskName 'GoldSweep - MetaTrader 5'"
    "  Start-ScheduledTask -TaskName 'GoldSweep - Bot'"
    if (-not $Paper) { "Then send /golive <your account number> to the bot on Telegram." }
}
