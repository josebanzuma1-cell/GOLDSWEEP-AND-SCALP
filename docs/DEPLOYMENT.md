# Running GoldSweep around the clock

GoldSweep trades only while three things run on a Windows PC: the **MetaTrader 5
terminal** (logged in, Algo Trading on), the **GoldSweep bot**, and Windows itself,
awake. A laptop that is shut down, put to sleep or logged off stops all three:
nothing new is opened and open trades are not managed (they keep their stop-loss
and take-profit at the broker). This guide makes GoldSweep restart by itself and
tell you on Telegram when something is wrong.

There are two ways to run it:

| | This PC or laptop | A Windows VPS (recommended) |
|---|---|---|
| Runs when | the PC is on, awake and logged on | always |
| Cost | none | about US$10-25 a month |
| Survives | restarts, crashes (with the tasks below) | the same, plus your PC being off |

A VPS is a Windows computer in a data centre that you reach with Remote Desktop.
Choose one with Windows Server or Windows 10/11, **2 GB of memory or more**, in a
region near your broker's servers (for Exness: Europe or Asia). Many brokers list
"Forex VPS" offers; any provider with a Windows image works.

## 1. Install GoldSweep (PC or VPS)

Follow **Setup** in `README.md` on the machine: Python 3.13, the project folder,
`.venv`, `pip install -r requirements.txt`, `.env` (MT5 login, Telegram, AI key),
then:

```powershell
python main.py --check        # MT5 account, symbol, sessions
python main.py --news         # calendar, headlines, AI
```

On a VPS, copy the project folder over Remote Desktop (or clone it), and copy your
`.env` the same way. Never e-mail it or put it in a shared folder: it holds your
MT5 password and API keys.

## 2. MetaTrader 5 that logs itself in

1. Install MetaTrader 5 and log in to the account (File > Login to Trade Account),
   ticking **Save password**. From then on the terminal logs in by itself.
2. Turn on **Algo Trading** (the toolbar button turns green) and tick Tools >
   Options > Expert Advisors > **Allow algorithmic trading**. Untick "Disable
   algorithmic trading when the account has been changed" and "... when the
   profile has been changed".
3. The account must be a **hedging** account (the window title says "Hedge"):
   GoldSweep splits each trade into separate positions, which a netting account
   would merge. `--live` refuses a netting account.

## 3. Keep Windows awake and logged on

- **Power**: Settings > System > Power: when plugged in, sleep after **Never**;
  set "closing the lid" to **Do nothing** on a laptop. Keep a laptop plugged in.
- **Updates**: Settings > Windows Update > Advanced options > Active hours: set
  them to cover your trading hours, so restarts happen outside them.
- **Automatic logon** (needed after a restart, because MT5 needs a desktop): on a
  VPS, use Microsoft's **Autologon** tool (Sysinternals): run it, enter your user
  name and password, press Enable. The password is stored encrypted by Windows.
  On a personal laptop, weigh this against the security of the laptop: anyone who
  can switch it on is then logged in.
- **Remote Desktop on a VPS**: when you leave, **close the Remote Desktop window**
  (the session stays logged on). Do not use Start > Sign out: that logs the user
  off and stops MT5 and GoldSweep.

## 4. Let Task Scheduler start everything

From the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1            # live (demo)
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 -Paper     # paper trading
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 -Dashboard # also open the dashboard
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 -DryRun    # show, create nothing
```

It creates, for your Windows user (no administrator rights needed):

| Task | Does |
|---|---|
| GoldSweep - MetaTrader 5 | starts the terminal at logon |
| GoldSweep - Bot | starts the bot 90 s after logon, **without a console window**, and checks every 5 minutes: if the bot has stopped for any reason it is started again |
| GoldSweep - Watchdog | every 15 minutes: a Telegram alert if the bot has gone quiet while the gold market is open, and an all-clear when it is back |
| GoldSweep - Dashboard | (with `-Dashboard`) opens the dashboard at logon |

The scheduled bot runs `python main.py --live --unattended`: nobody is at the
keyboard to type the confirmation, so it **starts watching only** and asks on
Telegram. Send

```
/golive <account number>
```

(your MT5 account number, e.g. `/golive 12345678`) to allow live orders. Until then it alerts but sends no
order of any kind, except `/stop`. This happens after every restart: you always
decide when the bot may trade.

Start the tasks now without logging off and on:

```powershell
Start-ScheduledTask -TaskName "GoldSweep - MetaTrader 5"
Start-ScheduledTask -TaskName "GoldSweep - Bot"
```

To stop the scheduled bot (and keep it stopped), double-click `deploy\stop-bot.cmd`;
`deploy\start-bot.cmd` starts it again. To remove everything:
`powershell -ExecutionPolicy Bypass -File deploy\uninstall-tasks.ps1`.

Do not run the scheduled bot and `Start GoldSweep LIVE.cmd` at the same time:
use one or the other.

## 5. What you will see on Telegram

| Message | Means |
|---|---|
| 🚀 ... started ... watching only. To allow live orders, send /golive ... | a (re)start; send `/golive <account>` to trade |
| ✅ Live orders confirmed ... | your confirmation was accepted |
| 💓 ... running \| trading active \| equity ... (quiet, hourly) | still alive; `alerts.heartbeat_minutes` sets how often (0 = off) |
| ⚠️ GoldSweep has not been seen for N min | the bot stopped while the market is open: check the PC / VPS |
| ✅ GoldSweep is running again | the alert is over |

The watchdog runs on the same machine as the bot, so it cannot report a machine
that is off or asleep. If the hourly heartbeat stops arriving, the machine itself
is down: that is the signal to check it (or to move to a VPS).

## 6. Upkeep that runs by itself

- **Logs**: `logs\goldsweep.jsonl` rotates every day and keeps 30 days; log rows
  in the database older than `housekeeping.log_days` (30) are removed daily.
- **Backups**: every day at `housekeeping.backup_time_utc` (00:15 UTC) a copy of
  the database goes to `data\backups`; the newest 14 are kept. To restore one,
  stop the bot and copy it over `data\goldsweep.sqlite3`.
- **Updates to GoldSweep**: stop the bot (`deploy\stop-bot.cmd`), replace the
  code, run `python -m pytest`, then `deploy\start-bot.cmd`.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| "Algo Trading is off in MetaTrader 5" | press the Algo Trading button (green); see step 2 for the options that switch it off |
| "is a netting account" | open a hedging demo account (step 2) |
| MT5 error -10005 (IPC timeout) | the terminal is open but not logged in: log in with Save password |
| A console window's title starts with "Select" | a click paused it: press Esc (the scheduled bot has no window, so this cannot happen to it) |
| No 💓 for over an hour | the PC is off, asleep or logged off (step 3) |
| ⚠️ alerts after every restart | normal until the bot is back; on a VPS, check automatic logon |
| "Not starting: an unattended start needs Telegram" | fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in `.env` |
