<#
.SYNOPSIS
    Installs or updates the GoldSweep chart panel (mt5\GoldSweepPanel.mq5) in MetaTrader 5.

.DESCRIPTION
    For every MetaTrader 5 data folder on this PC (%APPDATA%\MetaQuotes\Terminal\<id>\,
    the ones with an origin.txt), it:
      1. keeps the panel already there as MQL5\Indicators\GoldSweepPanel.prev.mq5;
      2. copies this project's mt5\GoldSweepPanel.mq5 into MQL5\Indicators;
      3. compiles it with MetaEditor and prints the result (0 errors expected).
    An indicator already on a chart reloads by itself after the compile. To attach it
    the first time: Navigator (Ctrl+N) > Indicators > right-click > Refresh, then drag
    GoldSweepPanel onto the XAUUSD chart (README: "Load the chart panel in MT5").

    The panel never trades; it reads the bot's files in Common\Files\GoldSweep and
    writes only scalp_commands.json there when you press its buttons.

.PARAMETER MetaEditor
    MetaEditor64.exe of the MetaTrader 5 installation.
.PARAMETER DataRoot
    Where the terminals keep their data folders.
.PARAMETER DryRun
    Show what would be copied and compiled, change nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-panel.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-panel.ps1 -DryRun
#>
param(
    [string]$MetaEditor = "C:\Program Files\MetaTrader 5\MetaEditor64.exe",
    [string]$DataRoot = (Join-Path $env:APPDATA "MetaQuotes\Terminal"),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$source = Join-Path (Split-Path -Parent $PSScriptRoot) "mt5\GoldSweepPanel.mq5"
if (-not (Test-Path $source)) {
    Write-Host "Not found: $source (run this from the GoldSweep project)."
    exit 1
}
if (-not (Test-Path $DataRoot)) {
    Write-Host "No MetaTrader 5 data under $DataRoot. Start MetaTrader 5 once, then run this again."
    exit 1
}
$terminals = @(Get-ChildItem $DataRoot -Directory | Where-Object {
    (Test-Path (Join-Path $_.FullName "origin.txt")) -and (Test-Path (Join-Path $_.FullName "MQL5"))
})
if ($terminals.Count -eq 0) {
    Write-Host "No MetaTrader 5 data folder found under $DataRoot. Start MetaTrader 5 once, then run this again."
    exit 1
}
$canCompile = Test-Path $MetaEditor
$failed = 0
foreach ($terminal in $terminals) {
    $origin = (Get-Content (Join-Path $terminal.FullName "origin.txt") | Select-Object -First 1)
    $folder = Join-Path $terminal.FullName "MQL5\Indicators"
    $target = Join-Path $folder "GoldSweepPanel.mq5"
    $previous = Join-Path $folder "GoldSweepPanel.prev.mq5"
    Write-Host "Terminal $($terminal.Name) ($origin)"
    if ($DryRun) {
        if (Test-Path $target) { Write-Host "  would keep the current panel as $previous" }
        Write-Host "  would copy $source"
        Write-Host "  to $target"
        if ($canCompile) { Write-Host "  would compile it with $MetaEditor" }
        continue
    }
    New-Item -ItemType Directory -Force $folder | Out-Null
    if (Test-Path $target) { Copy-Item $target $previous -Force }
    Copy-Item $source $target -Force
    Write-Host "  copied to $target"
    if (-not $canCompile) {
        Write-Host "  MetaEditor not found: MetaTrader 5 compiles it when you attach it."
        continue
    }
    $log = Join-Path $env:TEMP "GoldSweepPanel-compile.log"
    Remove-Item $log -ErrorAction SilentlyContinue
    Start-Process -FilePath $MetaEditor -ArgumentList "/compile:`"$target`"", "/log:`"$log`"" -Wait
    $result = @(Get-Content $log -Encoding Unicode -ErrorAction SilentlyContinue | Where-Object { $_ -match "^Result" })
    if ($result.Count -eq 0) {
        Write-Host "  no compile result (see $log)"
        $failed++
    } else {
        Write-Host "  $($result[-1])"
        if ($result[-1] -notmatch "^Result: 0 errors") { $failed++ }
    }
}
if ($DryRun) { Write-Host "Dry run: nothing was changed." }
exit $failed
