# GoldSweep

A trading assistant for **XAUUSD (spot gold) only**: it reads prices from
MetaTrader 5, tracks liquidity pools and session kill zones, looks for a
liquidity sweep followed by a market-structure shift, and alerts on (or
optionally executes) the entry under strict risk rules.

> **Risk notice.** No trading system is loss-free. GoldSweep is built to keep
> losses small and controlled and to seek positive expectancy. It cannot
> promise either, and nothing here is financial advice. Run it on a **demo**
> account until you have seen it behave for a long time.

GoldSweep runs on the **Windows PC or VPS that hosts the MetaTrader 5
terminal**. It does not run on a phone; the MT5 mobile app only lets you watch
the same account.

## Status

The build follows the phases in [`docs/SPEC.md`](docs/SPEC.md), with a stop for
review after each one.

| Phase | Contents | State |
|---|---|---|
| 1 | Settings, clock and sessions, logging, SQLite, mock broker, MT5 client, `--check` | built, tested, approved |
| 2 | Analysis engine: swings, liquidity pools, sweeps, MSS, FVG / IFVG / order block, CRT, bias, scoring | built, tested, approved |
| 3 | Backtester with walk-forward testing, risk rules, buyer/seller pressure meter | built, tested, approved |
| 4 | Economic calendar, headlines, AI classifier, news bias, scenario briefs, news blackout | built, tested, approved |
| 5 | Telegram alerts and commands, paper trading live on the broker feed, kill switch | built, tested, approved |
| 6 | Live execution on a demo account, three-target split, on-chart panel | built, tested, approved |
| 7 | Streamlit dashboard: chart, scenarios, news, trades, risk, equity, backtests, kill switch | built, tested, approved |
| 8 | Unattended running (Task Scheduler, /golive), heartbeat, watchdog, backups, VPS guide | built, tested, approved |
| 9 | More trades and "sniper" entries: (1) entries that fill and never wait into a blocked time, (2) M1 refinement, (3) more setup models | built and tested: entries wait 2 hours (D82), M1 refinement left off (D83), breakeven only at TP1 (D84), new models alert only (D85) |
| 10 | Session playbooks: A+ grades, per-session strategy from the evidence, monitoring before auto-trading | built, tested, approved: candidates from the analysis (D86), shadow-book monitoring, `/approve`, Asia on (D87) |
| S | Scalper add-on alongside the main strategy (`docs/SCALPER_SPEC.md`): M1 scalps, step trail, three-child ladder, chart dashboard, MT5 assistant | S0 approved (Python engine, D89); S1 (logic, tests, first backtest) built: it loses after costs in every variant (D101), so it stays off. Research on tick ("HFT"), intraday and swing modes with a switch between them: `docs/SCALPER_MODES.md` (D102-D107), approved. S2 (the switch, the tick recorder, latency, costs; D108) approved. S3 (intraday mode) built and tested: all three rules lose after costs, so the mode does not run (D109), approved. S4 (swing mode: Donchian, momentum, pullback, swap charged) built and tested: negative over 2014-2022, positive only in the 2022-2026 rise, and a 0.01 lot needs about 76,000 at 0.25%; the mode does not run (D110), approved. Scalper v3 (D111): HFT on M1, intraday on M5, swing scalping on M15, each tested on paper then on the demo account (backtests reported, not gates); tick recorder off. S5 (HFT mode: runner, scalper paper book and demo executor with its own magic number, account guard, no opposite positions) built and tested; its backtest loses 0.12 R a scalp after costs (D111); approved, on paper then the demo account. S6 (intraday on M5, swing scalping on M15, one mode interface for the bot and the new mode backtest, a book and magic number per mode) built and tested: intraday -0.04 R a trade after costs (flat before), swing scalping -0.07 R (D112); approved. S7 (chart panel v2 from `scalp.json` with Pause / Resume / Scalper off buttons, the dashboard's Scalper tab, Telegram `/stats`; D113) built, tested and approved. S8 (chart panel v3: graded signals in the reference layout, setups forming with "A+ possible", pools and gaps, each scalper mode's layer, analysis box, switch buttons, chart theme, A+ alerts, `deploy\install-panel.ps1`; D114) built, tested and approved. S9 (one, two or all three modes side by side with shared rules per account; HFT as one trade in five parts at 0.25 R steps, 0.25% risk for all five, only in the direction of a live A or A+ setup and no longer once that setup is done; chart panel v3.1 toggles and TP4/TP5; D115) built and tested: the five-part ladder still loses 0.12 R a scalp after costs in the backtest, which cannot apply the A filter; awaiting approval |

Only `python main.py --live` sends orders to a broker, and only after
`MODE=LIVE` in `.env`, `mode: LIVE` in `config.yaml` and the account number typed
at startup. The backtester and the paper trader simulate trades only.

## How a signal is found

The analysis engine (`goldsweep/analysis/`) follows the workflow of the spec and
of the owner's three study guides (market-structure shift, fair value gaps,
liquidity), on closed bars only, so a backtest and a live run see the same thing:

1. **Bias** (H1): swing structure, premium or discount against the previous
   day's midpoint, and the news score. LONG, SHORT or NEUTRAL.
2. **Liquidity** (M15): Asian, London and New York ranges, previous day and
   week, equal highs and lows, unswept swing points, trendlines, round numbers,
   and the range of the last four-hour candle (Candle Range Theory).
3. **Sweep** (M5): price trades through a pool and closes back inside.
4. **Market-structure shift**: a close through the last opposing swing, carried
   by a displacement candle.
5. **Entry**: the middle of the fair value gap the displacement left (or an
   inversion gap, or the order block), as a limit order that waits up to two
   hours (`entry_expiry_bars: 24`, D82). Stop beyond the sweep's extreme,
   targets at the next external liquidity, rejected below 2R. `entry_mode` can
   also place it at the gap's near or far edge or at the optimal trade entry
   (`ote`, 70.5% back). Optional "sniper" entries (`strategy.refinement.mode: m1`)
   wait for price to turn inside the gap on one-minute bars and enter with a
   tighter stop; they are off because they did worse on 2024-2026 (D83).
6. **Score** 0-100 from ten components. 60 or more is worth an alert. The
   spec trades from 75; on the demo account `auto_trade_threshold` is set to 60
   so that enough trades happen to judge the strategy (D81). A neutral H1 bias
   adds 5 to both.

**More setup models and confluence (D85).** Besides this model, three more look
for setups on the same bars: `cisd` (confirmed by a change in the state of delivery
instead of the shift), `silver_bullet` (only in the New York 03-04, 10-11 and 14-15
hours) and `continuation` (sweeps of internal pools, with the H1 trend). On
2024-2026 none of them did as well, so they send **alerts only** (`config.yaml
models:`). Every alert also lists its confluences: Unicorn (the gap sits on a
breaker block), balanced price range, Judas swing (the raid ran beyond the New
York midnight open), Silver Bullet hour, OTE, and gold-silver SMT divergence. They
are recorded, not scored, so the demo account can show which ones matter.

**Session playbooks (D86).** Each session (Asian/Tokyo, London, New York) has a
playbook in `config.yaml playbooks:`: the rule that auto-trades there now, and an
A+ *candidate* found by `python -m goldsweep.playbook`, which tries four models
(sweep + MSS, CISD, continuation, and a breakout model) under score, confluence and
trend conditions (daily, 4-hour and 200 EMA trend) and keeps a rule only if it was
best on the first two-thirds of the history and also positive on the last third.
On 2024-2026 that gave London and New York the same A+ rule (the current model
with the Judas tag) and Asia none. A candidate is monitored before it may trade.

**Monitoring and approval (D87).** Every alert carries its grade (A+, A or B). A
setup that meets its session's candidate is also traded on a *shadow book*: on
paper only, by the same rules as every other trade, never sent to MetaTrader 5.
Monitored trades are counted quietly (`/status` shows the tally). After 20 have
finished with a positive average, Telegram asks the owner to send
`/approve <session>`; from then on only that session's A+ setups trade there.
`/revoke <session>` undoes it, and editing a candidate in `config.yaml` starts its
count again. The Asian session is switched on: its setups are graded and alerted,
and nothing trades there until it has a candidate that passes monitoring.
`/status` shows each session's playbook and tally.

**The scalper and the switch (D88-D112).** A scalper runs beside the main strategy
with three modes, all entering in the active sessions (London, New York and their
overlap): **HFT** on one-minute bars (S5); **intraday** on M5, the sweep and reclaim plus
the New York opening range and VWAP trend, not against the H1 trend, targets 1/2/3 R,
flat by the New York rollover; and **swing scalping** on M15, pullbacks to the 20 EMA
and Asian/London range breakouts with the H1 trend, a 20-bar channel trail, held up
to two days and never over the weekend (S6). Each mode keeps its own paper book and
demo executor (its own magic number), and a mode switched away from still manages its
open scalp to the end. `/mode main|scalper|both` chooses which strategies may open trades and
`/scalper off|hft|intraday|swing` chooses the scalper's mode; one, two or all three run
side by side with `/scalper hft,swing` or `/scalper all` (S9; also on the dashboard's
Control tab and the chart panel's toggles). Side by side, the modes share one set of
rules per account: no scalp against another mode's open scalp, a second scalp the same
way only once the first is risk-free, and one daily loss limit (-4 R), 8 scalps a day
and the 3-losses pause for all modes together. A selected mode runs on the scalper's own **paper** book (a notional
10,000) on the live feed; `/approve scalper <mode>` sends its scalps to the **demo
account** with the scalper's own magic number, and `/revoke scalper <mode>` returns it
to paper. Every scalp carries a stop-loss and its take-profits, one scalp at a time per
mode. **HFT** (S9) is one trade in five parts with 0.25% risk for all five together,
targets at 0.25/0.5/0.75/1.0/1.25 R (wider when the costs need it), and it trades only
the direction of a live A or A+ setup of the main strategy: one graded in the last 60
minutes that has not reached its target, failed back through its sweep or closed its
trade, or one forming now that can still be A+. Once that setup is done, HFT stops
taking new scalps that way; an open scalp finishes under its own stop, targets and
15-minute time stop. Breakeven-plus at TP1, then the stop to TP k-1 after each TP k,
and the step trail manage it. The scalper never holds the side opposite the main
strategy: a main order first closes an opposite scalp. Messages are two per scalp
(placed, closed); `/status` lists open scalps and the last 24 hours. Backtests are
reported, not gates (D111): on 2024-2026 HFT lost 0.12 R a scalp after costs, intraday
0.04 R (flat before costs) and swing scalping 0.07 R (D111, D112:
`python -m goldsweep.scalping.mode_backtest --mode intraday --from 2024-01-01 --to
2026-10-31`, likewise `--mode swing` and `--mode hft`); paper and the demo account are
where each mode is judged. The current 1,071 demo account is too small for most
scalps (the minimum lot risks more than 0.25%); a 10,000 demo account is recommended. The tick recorder (D106) is off.
`/status` shows the latency: the terminal's ping and how long recent orders took.

A score measures how complete a setup is, not how likely it is to win. Every
setup can lose; the stop-loss is what keeps that loss small. The choices made
here are explained in `DECISIONS.md` (D16-D25).

## Backtesting

```powershell
python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31             # real history
python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31 --with-m1   # plus M1 fills
python -m goldsweep.backtest --from 2026-01-05 --to 2026-01-30 --mock      # synthetic
python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31 --candidates  # the A+ playbooks as if approved
```

A backtest trades what the session playbooks trade (D87): setups outside a
session's trading rule are recorded as not selected. `--candidates` trades each
session's A+ candidate instead, to compare.

The first run downloads the M5 history from MetaTrader 5 into the SQLite
database (the terminal must be open and logged in); later runs use the stored
bars, and `--offline` forbids contacting MT5. In the terminal, set
Tools > Options > Charts > **Max bars in chart** to "Unlimited" first, or the
broker's history is cut short. Two years of M1 bars take about 100 MB of disk.

Replaying 2024 to 2026 (about 207,000 bars) through the signal engine took 15 minutes on this
laptop; the signals are cached, so the next report on the same data and settings
took 9 seconds. The report goes to `reports/backtest_<from>_<to>/`:

| File | Holds |
|---|---|
| `report.html` | Headline figures, equity curve, the session playbooks traded, results by playbook, session, weekday, direction and pressure meter, the walk-forward windows, every trade, and what the test cannot see |
| `trades.csv` | One row per trade: entry, exits, lots, costs, result in money and in R |
| `signals.csv` | Every signal and what happened to it (traded, below threshold, blocked by a risk rule, expired, ...) |
| `equity.csv`, `walk_forward_trades.csv`, `summary.json` | Equity after each trade, the out-of-sample trades, and every figure as JSON |

Trades are simulated on bid and ask prices with the bars' spread, 10 points of
slippage on market entries and stops, and no commission (the Exness standard
account charges none: its cost is the spread; set `commission_per_lot` for an
account that charges one, D108), under the
same risk rules the bot will use (0.5% risk per trade, daily and weekly loss
limits, trade counts, spread filter). Where the order of events inside a bar is
unknown, the worse case is assumed. The walk-forward test picks settings on 6
months and trades them on the following 2, rolling forward, and warns loudly
when the results look overfitted. Details: `DECISIONS.md` D26-D34.

**Buyer/seller pressure meter.** `--check` shows a reading such as
"Buyers 62% / Sellers 38%". It is an *estimate*: spot gold has no exchange
volume, so it combines where recent bars closed (weighted by MT5 tick volume),
the size of up and down candles, the H1 structure, and from Phase 4 the news
outlook. It does not change signal scores; every backtest reports how trades did
when the meter agreed with them and when it did not.

A backtest describes the past under stated assumptions. It cannot promise future
results; every trade can lose.

## News and AI

```powershell
python main.py --news            # calendar, headlines, news bias, scenario brief
python main.py --news --no-llm   # the same with keyword rules only: no API calls, no cost
```

The report refreshes the economic calendar and the headline feeds (each on its
own schedule, so running it often does not hammer the sources), classifies new
headlines, and prints:

- the US releases of the next 48 hours that **block new entries** (30 minutes
  before to 15 minutes after) and the post-news windows (15 to 90 minutes after);
- the **news bias** (-1 bearish to +1 bullish for gold), from classified
  headlines and from data surprises (actual against forecast), each fading with
  a 2-hour half-life;
- the buyer/seller **pressure meter** with its news part filled in;
- a **scenario brief**: a bullish, a bearish and a no-trade case built from the
  nearest liquidity pools, the H1 bias and today's releases;
- today's **AI usage and its estimated cost**.

Keys in `.env` (all optional; copy `.env.example` to `.env` first):

| Key | Gives | Without it |
|---|---|---|
| `LLM_PROVIDER` + its key | headline classification and scenario briefs by a language model (see below) | keyword rules and a scenario template |
| `FINANCECALENDAR_API_KEY` | the calendar with actual figures, so data surprises count (free key; 10 requests a day) | the Forex Factory weekly feed (no actual figures) |
| `FINNHUB_API_KEY` | Finnhub forex and general news | RSS only: Federal Reserve, ECB, FXStreet |

The language model is chosen in `.env` with `LLM_PROVIDER`:

| `LLM_PROVIDER` | Key | Default model | Cost and limits |
|---|---|---|---|
| `groq` | `GROQ_API_KEY` (free at console.groq.com/keys) | `openai/gpt-oss-20b` | free plan; GoldSweep keeps to 6 calls a minute and 180 a day |
| `anthropic` | `ANTHROPIC_API_KEY` (paid) | `claude-haiku-5-5` | fractions of a cent per headline; stops at `news.llm_daily_budget_usd` (US$0.50) a day |
| `openai_compatible` | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | none: set `LLM_MODEL` | any service with an OpenAI-style chat API and JSON schema output |

`LLM_MODEL` empty means the provider's default. Headlines without gold-related
words skip the model (`news.llm_prefilter`), which keeps a free plan's quota for
the stories that matter. When a cap or the budget is reached, or the service
refuses a request, the keyword rules take over until the next day or run. The
model only classifies text and writes briefs: it never places a trade. Calendar data from FinanceCalendar.com must be credited wherever it is
shown, and the report does so. Choices are explained in `DECISIONS.md` (D35-D44).

## Paper trading and alerts

```powershell
python main.py --paper            # paper trading on the MT5 demo feed; Ctrl+C stops it
python main.py --paper --mock     # the same on synthetic prices
python main.py --paper --no-llm   # keyword rules only (no API calls)
```

Every five minutes, two seconds after the M5 bar closes, the paper trader reads
the closed bars, manages its paper orders and positions, and runs the signal
engine. A setup that clears the auto-trade threshold becomes a **paper** order
if the risk rules allow it; one that clears the alert threshold is an alert. It
manages trades exactly as the backtester does: the same code fills, stops,
moves to breakeven, closes 50% at TP1, 30% at TP2 and the rest at TP3, and trails
the stop after TP1. It never sends an
order to MetaTrader 5. Its state is saved after every change, so after a
restart it carries on with the same orders and positions.

Alongside, it refreshes the calendar and headlines every minute, warns 15
minutes before each high-impact US release, sends a scenario brief 30 minutes
before the London and New York kill zones and 60 minutes before each release,
and sends a daily summary at 21:00 New York time.

**Telegram** (optional; without it the alerts print in the console):

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the token it
   gives you into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<your token>/getUpdates` in a browser and copy
   the number after `"chat":{"id":` into `TELEGRAM_CHAT_ID`. The chat id is a
   number; the token (digits, a colon, then letters) goes only in
   `TELEGRAM_BOT_TOKEN`.
3. Start `python main.py --paper`; the bot says it has started.

The bot answers only that chat:

| Command | Does |
|---|---|
| `/status` | trading state, equity, open positions and orders, today's result, news bias, each session's playbook and monitoring tally |
| `/pause` | no new trades; open trades are still managed |
| `/resume` | trade again |
| `/stop` | **kill switch**: cancels every order, closes every paper position, clears the monitored setups, stops trading until `/resume` |
| `/approve <session>` | after its monitoring is confirmed, the session (`asia`, `london`, `newyork`) trades only its A+ playbook (D87) |
| `/revoke <session>` | back to the session's trading rule; its A+ candidate is monitored again |
| `/mode main\|scalper\|both` | which strategies may open trades (D103); `/mode` alone shows it |
| `/scalper off\|hft\|intraday\|swing` | the scalper's modes: one, or several as `/scalper hft,swing`, or `/scalper all`; a selected mode runs on the scalper's paper book, and an unbuilt mode does nothing |
| `/approve scalper <mode>` | sends that mode's scalps to the demo account (a real account also needs the forward-test file, D93) |
| `/revoke scalper <mode>` | back to paper; scalps already open keep their stop-loss and take-profit |
| `/stats [days]` | results after costs per scalper mode (paper and demo, next to each backtest) and of the main strategy; 30 days unless given |
| `/help` | the list |

An alert looks like this:

```
🟢 XAUUSD LONG setup (score 81.1) — London kill zone
Swept: Asian low 2027.00 → MSS confirmed (M5)
Entry: 2028.90 (FVG mid, limit) | SL: 2026.09 | TP1: 2036.00 | TP2: 2048.00
R:R 2.5 / 6.8 | Risk 0.5% (0.17 lots)
News bias: +0.26 (Gold jumps as Fed signals rate cuts, 50m ago) | Next high-impact: USD CPI in 3h10m
Pressure: Buyers 58% / Sellers 42% (buyers)
Action: paper limit order placed, valid until 08:25 UTC
Mode: PAPER  |  id: 2026-11-17-LDN-01
```

🟢 a paper order was placed, 🟡 alert only, ⛔ blocked by a risk rule (the reason
is given). Every order carries its stop-loss and take-profit; a plan without
them cannot even be created. Choices are explained in `DECISIONS.md` (D45-D53).

## Live trading on the demo account (Phase 6)

```powershell
python main.py --live            # real orders on the MT5 account, after a typed confirmation
python main.py --live --no-llm   # the same with keyword rules for the news
```

**GoldSweep runs on the Windows PC (or a Windows VPS) next to MetaTrader 5, not
on the phone.** The phone shows the positions in the official MetaTrader 5 app
(log in with the same account) and gets the Telegram messages. If the PC is off
or the bot is stopped, open positions keep their stop-loss and take-profit at the
broker; nothing new is opened and nothing is moved until the bot runs again.

Starting live trading takes three deliberate steps:

1. In `.env`: `MODE=LIVE`.
2. In `config.yaml`: `mode: LIVE`.
3. Start `python main.py --live`. It shows the account, the balance, the risk per
   trade and the lot cap, and asks you to type `LIVE <account number>`. Anything
   else, or no answer, cancels: no order is sent.

It refuses to start on a real-money account (Phase 6 is for demo accounts;
`execution.allow_real_account` exists for later, after the demo results have
been reviewed) and when **Algo Trading** is off in MetaTrader 5 (the button in the
toolbar must be green; also tick Tools > Options > Expert Advisors > "Allow
algorithmic trading"). Set both switches back to `PAPER` to paper-trade again.

What happens to a signal that clears the auto-trade threshold (spec 7b):

- The risk rules are checked first (limits, spread, news blackout), then the size
  is worked out from the account balance so that a stop-out loses at most
  `risk_per_trade_pct` (0.5%), then `max_total_lots` and the margin check (no order
  that takes the margin level below 1000%).
- The trade is split into up to three positions with the same entry and stop:
  50% to TP1 (the nearest liquidity pool), 30% to TP2 (the next pool) and 20% to
  TP3 (3R from the entry). Small positions use fewer parts: the broker's minimum
  is 0.01 lots per part. **Every order carries its stop-loss and take-profit when
  it is sent.**
- A limit entry is cancelled when it expires (`entry_expiry_bars`), when price
  reaches TP1 first, or before any time a new trade is not allowed: a news
  blackout, the rollover block, the Friday cutoff or the weekend. The alert says
  so ("valid until 16:45 UTC (before the rollover block)"); paper trading and the
  backtester follow the same rule. When TP1 is hit, the other parts' stops move
  to breakeven; after TP1 they trail behind M5 swing points. (The spec also moved
  them at +1R in favour; on 2024-2026 that closed many trades at zero just before
  they ran, so `risk.breakeven_at_r` is 0: see D84.) As a high-impact release's
  blackout starts, a trade 1R up moves its stop to breakeven too.
- Every request and the broker's answer is stored in the `order_log` table;
  finished trades in `live_trades`. Telegram reports each placement, fill, target,
  stop, breakeven move and close. `/stop` closes every GoldSweep position at once.
- After a restart the bot reads what happened while it was off (fills, stops,
  targets) from MT5's deal history and carries on managing what is still open.
  GoldSweep positions it has no record of keep their own stop and target and are
  reported, not managed; one without a stop or target is closed for safety.

GoldSweep recognises its own orders by `execution.magic_number`; trades you open
by hand are never touched (except by `/stop`, which only closes GoldSweep's).

### The chart panel (GoldSweepPanel.mq5)

While the bot runs (paper or live) it writes two files to MetaTrader 5's shared
`Common\Files\GoldSweep` folder: `signals.json` after every M5 bar and `scalp.json`
every minute. The indicator `mt5/GoldSweepPanel.mq5` (version 3.1, S8-S9, D114-D115) draws
them on the gold chart in the GoldSweep look:

- **Signals**: a tag such as `SELL A+ 82` (gold for A+), the trade box with its risk
  and reward zones, `ENTRY / SL / TP1 / TP2 / TP3` labelled at the box's edge, an "x"
  at the sweep's extreme, the status, tags and playbook, and `+2.10 R (tp2)` once the
  trade closed. The "Lowest grade drawn" setting picks A+ only, A and A+, or all.
- **Upcoming setups**: a raid that has swept a pool but is not a signal yet is marked
  `FORMING BUY: Asian low 2027.00 | swept: waiting for the structure shift`, in gold
  with "A+ possible" when the session's A+ rule can still be met. It is a mark to
  watch, not an order; many raids never become signals.
- **Levels**: the nearest liquidity pools above and below price, and open fair value
  gaps.
- **Scalper**: the HFT step line and its "x" marks, each scalp's box with its targets
  (TP1-TP5 for HFT), TP hits and result, and each selected mode's own layer (intraday: VWAP, Asian and New York
  opening ranges; swing: the M15 20 EMA, its 20-bar channel, Asian and London
  ranges).
- **Dashboard** (bottom-left): mode and PAPER/DEMO badge, buttons, the status and
  "Quick Scalp" line (what the selected mode did on its last bar and why), the market,
  the open scalp and the results after costs. It shows STALE when the bot stops
  rewriting its file.
- **Analysis box** (top-left), with page buttons:
  - **Market**: H1 bias, news bias, pressure estimate, session and next window, next
    release;
  - **Levels**: the nearest pools and gaps;
  - **Setups**: what is forming and the last signals;
  - **Scalper**: every mode's status;
  - **Today**: results;
  - **Brief**: the latest scenario brief, written by the AI as text only.
- **Alerts**: a pop-up and sound (and, if you switch it on, a push to the MT5 phone
  app) for each new A+ signal and each new setup that can still be A+.
- **Buttons**: Pause/Resume; Trade: Main / Scalper / Both; Scalper: Off / HFT /
  Intraday / Swing, where HFT, Intraday and Swing each switch one mode on or off, so
  one, two or all three can run (Off clears them). They write one line to `scalp_commands.json`, which the bot
  carries out within a few seconds and reports on Telegram. That file is the only
  thing the indicator writes. It never trades, and the kill switch and `/approve`
  stay on Telegram and the dashboard.
- **Theme**: on by default, the charcoal background with off-white and gold candles,
  and the tick-volume histogram. Your own colours come back when you remove the
  indicator.

#### Load it in MetaTrader 5

1. Start the bot (`Start GoldSweep LIVE.cmd` or `--paper`): it writes the files the
   panel reads.
2. Install it. Either run
   `powershell -ExecutionPolicy Bypass -File deploy\install-panel.ps1`, which copies it
   into every MT5 terminal on this PC, keeps the old one as `GoldSweepPanel.prev.mq5`
   and compiles it (`-DryRun` only shows what it would do). Or, by hand, in MetaTrader
   5 use File > Open Data Folder, open `MQL5\Indicators` and copy
   `mt5\GoldSweepPanel.mq5` there.
3. In MetaTrader 5 open the Navigator (Ctrl+N), right-click **Indicators** and choose
   **Refresh**.
4. Open an XAUUSDm chart: M1 for HFT, M5 for the main strategy and intraday, M15 for
   swing. Drag **GoldSweepPanel** onto it. If an older version is already on the
   chart, it reloads by itself after step 2.
5. In the **Inputs** tab choose what to see. The groups are Files, Chart theme, Main
   strategy (lowest grade, forming setups, pools, gaps), Scalper (layers: the
   selected mode, all or none), Panels, Alerts and Colours. No DLLs and no WebRequest
   are needed. Press OK.
6. Keep the layout: Charts > Template > Save Template, name it `GoldSweep` (or
   `default` so every new chart opens with it).
7. For alerts on your phone: install MetaTrader 5 on the phone, copy its MetaQuotes
   ID (Settings > Chat and messages), enter it on the PC under Tools > Options >
   Notifications, then set the panel's input **Also to the MT5 phone app** to true.

If the panel says "waiting for signals.json", the bot is not running or has not
finished a bar yet. If the dashboard says STALE, the bot has stopped writing; check
its window. The panel draws only on gold charts (XAUUSD / GOLD).

Choices are explained in `DECISIONS.md` (D56-D66, D99, D113, D114).

## Dashboard (Phase 7)

```powershell
python main.py --dashboard          # opens http://localhost:8501 in the browser
python main.py --dashboard --mock   # the same for the --mock database
```

Or double-click **Open GoldSweep Dashboard** in the project folder. Keep its
window open while you use the page; closing it stops only the dashboard, never
the trading bot. The page refreshes itself (every 30 seconds by default).

| Tab | Shows |
|---|---|
| Market | the M5 chart with liquidity pools (dashed; grey once swept), fair value gaps price has not filled (shaded) and signals with their entry, SL and TP1-TP3; the buyer/seller pressure estimate; the nearest pools; today's scenario briefs |
| Trades | open positions and waiting entry orders, the risk status against every limit, the equity curve and results, closed trades, and (live) every order sent to MT5 with the broker's answer |
| Scalper | the scalper's mode and where it trades, each mode's paper balance, open scalps, results per mode and account (1, 7, 30 or 90 days) next to each mode's backtest, the running total R per mode, and every closed scalp |
| News | releases that block new entries, headlines with their AI tags (direction, impact, drivers, why), today's AI usage |
| Signals | alerts, trades and blocked setups of the last day, week or month |
| Backtests | each report in `reports/`: its figures, and the full HTML report to download |
| Control | Pause, Resume, Ask for /status, and the **kill switch** (tick the box, then "Close everything and stop") |

The dashboard only **reads** the bot's database: the running bot (`--paper` or
`--live`) stores its candles and a snapshot of what it sees after every M5 bar.
The Control buttons put a command in the database; the bot carries it out within
a few seconds and reports it on Telegram too, so only the bot ever talks to
MetaTrader 5. When the bot is not running, the page says so and the buttons are
off (positions keep their stop-loss and take-profit at the broker; close them in
MT5 if needed). The page is served on this PC only (`localhost`), and Streamlit's
usage statistics are switched off. Choices are explained in `DECISIONS.md`
(D67-D72).

## Running it around the clock (Phase 8)

GoldSweep trades only while the PC is on, awake and logged on, with MetaTrader 5
open. **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** explains how to run it on this
PC or, better, on a Windows VPS:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1   # Task Scheduler tasks
python main.py --live --unattended   # what the scheduled bot runs: no console question
python main.py --watchdog            # one check: Telegram alert if the bot has gone quiet
```

- **Restarts by itself**: Task Scheduler starts MT5 and the bot at logon and
  starts the bot again within 5 minutes if it stops. The scheduled bot has no
  console window, so a stray click cannot pause it.
- **You still confirm live trading**: after an unattended start the bot watches
  only and asks on Telegram; `/golive <account number>` allows live orders. Until
  then it sends no order of any kind, except `/stop`.
- **Heartbeat**: a quiet "💓 running" message every hour, and a ⚠️ alert from the
  watchdog when the bot has gone quiet while the gold market is open.
- **Upkeep**: logs rotate daily (30 days kept), a database backup is made every
  day in `data\backups` (14 kept), and old log rows are removed.
- **Safety**: `--live` refuses a netting account, on which GoldSweep's split into
  separate positions could not work.

Choices are explained in `DECISIONS.md` (D73-D79).

## Requirements

- Windows 10 or 11 (the `MetaTrader5` Python package exists only for Windows).
- Python 3.11 or newer; 3.13 is recommended.
- For the real check: the MetaTrader 5 terminal, **open and logged in to a demo
  account** (File > Open an Account). A terminal with no account logged in does not
  answer, and the check says so after about a minute. The tests and
  `--check --mock` need neither MT5 nor an account.

## Setup

The whole path, on a fresh Windows PC or VPS:

1. Install **Python 3.13** (python.org; tick "Add python.exe to PATH") and
   **MetaTrader 5** from your broker. Log in to a **demo, hedging** account with
   Save password ticked, and turn Algo Trading on.
2. Put this project folder on the machine and create the environment (below).
3. Fill in `.env` (copied from `.env.example`): MT5 login, `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` (see Paper trading and alerts), `GROQ_API_KEY` (free) or
   another AI key. Leave `MODE=PAPER` for now.
4. `python main.py --check`, then `python main.py --news`, then a few days of
   `python main.py --paper` while you watch Telegram and the dashboard.
5. Install the chart panel (Live trading > The chart panel).
6. For the demo run: `MODE=LIVE` in `.env`, `mode: LIVE` in `config.yaml`, then
   `python main.py --live` (or double-click `Start GoldSweep LIVE`).
7. To keep it running without you: docs/DEPLOYMENT.md.

In PowerShell, from this folder:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Then:

```powershell
python main.py --check --mock      # offline: synthetic prices, no MT5 needed
python main.py --check             # real: MetaTrader 5 demo account
python main.py --check --date 2026-03-09   # session table for another day
python -m pytest                   # run the tests
```

`--check` prints the mode, the account, the resolved gold symbol with its
contract details, the current quote and spread, and today's session windows in
UTC and in your display time zone. Connecting to MetaTrader 5 can take up to a
minute. The exit code is 0 when everything worked, 1 when the broker step failed
or the server-time setting contradicts the broker's live clock, and 2 when the
configuration is invalid.

## Configuration

| File | Holds | Notes |
|---|---|---|
| `config.yaml` | Strategy, session and risk parameters | Validated strictly: an unknown key or an out-of-range value stops the program at startup. Quote clock times (`"15:00"`). |
| `.env` | Secrets and machine settings (MT5 login, API keys, `MODE`) | Never committed. Copy it from `.env.example`. Blank values count as "not set". |

**PAPER and LIVE.** PAPER is the default and never sends an order. LIVE takes
effect only when `MODE=LIVE` in `.env` **and** `mode: LIVE` in `config.yaml`
agree, and after a typed confirmation: `LIVE <account>` at the console, or
`/golive <account>` on Telegram after an unattended start.

**MT5 login.** Leave `MT5_LOGIN`, `MT5_PASSWORD` and `MT5_SERVER` blank to use
the account the terminal is already logged in to; then no password is stored.

**Broker server time.** MetaTrader 5 reports times in the broker's server
clock, not UTC. GoldSweep converts them, and `--check` shows the rule it is
using on the `Server time` line. Compare that with the clock in the MT5 Market
Watch window. If your broker follows EU daylight-saving dates, set
`broker.server_timezone: "Europe/Athens"` in `config.yaml`. Details are in
`DECISIONS.md` (D2).

**Sessions.** Each window is written in its own local time zone and converted
to UTC for each day, so daylight-saving changes need no edits.

## Project layout

```
main.py                      entry point
config.yaml, .env.example    configuration
goldsweep/
  cli.py                     the --check report
  core/settings.py           loads and validates config.yaml and .env
  core/clock.py              UTC clock, session calculator, trading-window gate
  core/server_time.py        broker server time <-> UTC
  core/logging.py            JSON logs to file and SQLite
  core/types.py              Mode and Timeframe
  analysis/periods.py        trading days (17:00 New York), anchored higher-timeframe bars
  analysis/indicators.py     true range and ATR
  analysis/structure.py      swings, trend, BOS / MSS, displacement
  analysis/zones.py          fair value gaps, inversion gaps, order blocks
  analysis/liquidity.py      liquidity pools and sweep detection
  analysis/crt.py            Candle Range Theory
  analysis/bias.py           higher-timeframe bias
  analysis/signals.py        the signal engine: sweep -> MSS -> entry -> score
  analysis/pressure.py       buyer/seller pressure meter (an estimate)
  risk/risk_manager.py       position sizing and the risk limits
  backtest/                  history download, simulator, metrics, walk-forward, report
  data/calendar.py           economic calendar sources, cache, blackout windows
  data/news.py               Finnhub and RSS headlines
  data/http.py               HTTPS GET for the news layer
  ai/news_analyzer.py        AI headline classifier with keyword fallback
  ai/news_bias.py            time-decayed news bias, data surprises
  ai/scenarios.py            pre-session scenario briefs
  ai/llm.py                  model providers (Claude, Groq, OpenAI-style), caps, usage log
  news_report.py             the --news report
  execution/book.py          the trade book and trade rules shared by backtests, paper and live
  execution/executor.py      the paper executor (saves its state), trade storage
  execution/mt5_gateway.py   MT5 orders: order_check, filling modes, retries (sends orders)
  execution/live_executor.py live trading: the split, management, reconciliation
  execution/panel.py         signals.json for the chart panel
  alerts/telegram.py         Telegram messages and commands
  alerts/messages.py         alert texts
  live.py                    the trading loop (--paper and --live)
  dashboard/                 the Streamlit dashboard (--dashboard): data, charts, page
  watchdog.py                the --watchdog check (Telegram alert when the bot is quiet)
deploy/                      Task Scheduler install / uninstall, start-bot and stop-bot
docs/DEPLOYMENT.md           running around the clock: VPS, tasks, heartbeat, upkeep
  data/broker.py             broker interface, symbol resolution
  data/candles.py            candle frame format, validation, aggregation
  data/mt5_client.py         MetaTrader 5 client
  data/mock_broker.py        offline broker with synthetic prices
  data/factory.py            picks mock or MT5
  storage/db.py              SQLite with migrations
mt5/GoldSweepPanel.mq5       the on-chart panel (an MQL5 indicator; read-only)
tests/                       pytest suite (uses a fake MetaTrader5 module)
docs/SPEC.md                 the build specification
DECISIONS.md                 choices made where the spec was open
```

Runtime files are created on first use and are git-ignored: `data/` (the SQLite
database `goldsweep.sqlite3`, and `goldsweep-mock.sqlite3` for `--mock` runs so
synthetic prices never mix with real ones) and `logs/` (one JSON object per line,
rotated daily).

## Tests

```powershell
python -m pytest
python -m pytest --cov        # with a coverage report
```

The suite runs without MetaTrader 5 and on any operating system: the MT5 client
is tested against an in-memory fake of the `MetaTrader5` module that reproduces
its server-time behaviour.
