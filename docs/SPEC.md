# GoldSweep build specification

This is the owner's original build prompt, saved verbatim on 2026-11-15 so that
every phase is built against the same text. Where the build had to choose
between readings, the choice is recorded in `DECISIONS.md`.

---

# Claude Code Build Prompt — "GoldSweep": XAUUSD News + Liquidity-Sweep Alert & Auto-Trading System

## 0. Role and ground rules

You are a senior Python quant developer. Build a production-quality, modular trading assistant for **XAUUSD (spot gold) only**. Work in phases (Section 9). At the end of each phase: run the tests, show me the results, summarize what was built, and **stop and wait for my approval** before the next phase.

Non-negotiable rules:
1. **No system is loss-free.** Never write code, comments, UI text or docs that claim "no losses", "guaranteed profit" or fixed win rates. The goal is *small, controlled losses and positive expectancy*.
2. **Every trade must have a hard stop-loss and take-profit attached at order send.** No martingale, no grid, no averaging down, no hedging to "recover" losses.
3. **Default mode is `PAPER`.** Live order execution requires `MODE=LIVE` in `.env` **and** a typed confirmation at startup. Build and test everything on a **demo MT5 account** first.
4. All times are handled internally in **UTC**, with sessions computed via `zoneinfo` so daylight-saving shifts are automatic. Never hard-code UTC offsets for sessions.
5. Secrets only in `.env` (never committed). Provide `.env.example`.
6. Write type-hinted, documented code with `pytest` tests for every module. Log everything (structured JSON logs + SQLite).
7. If any requirement is ambiguous, pick the safer option and note it in `DECISIONS.md`.

## 1. What the app does (summary)

1. Pulls XAUUSD price data from MetaTrader 5.
2. Pulls the economic calendar and live market news, and uses a low-cost LLM to classify each headline's likely impact on gold (bullish / bearish / neutral, high / medium / low).
3. Builds **pre-session scenarios** ("If US CPI comes in hot → USD up → gold likely sells off into Asian low liquidity, watch for a sweep and reversal at…").
4. Tracks **liquidity pools** (Asian range high/low, previous day high/low, equal highs/lows, session highs/lows, round numbers).
5. Detects **liquidity sweeps** followed by a **market-structure shift (MSS)** and an entry zone (**fair value gap / order block**) — the "best entry after liquidation".
6. Only acts during selected **session kill zones** (Tokyo/Asia, London, New York, London–NY overlap).
7. Sends **Telegram alerts** with entry, SL, TP, R:R, the reason and the news context.
8. Optionally **auto-executes** trades on MT5 under strict risk rules.
9. Includes a **backtester** and a **Streamlit dashboard**.

## 2. Tech stack (low-cost)

| Need | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Broker / prices / execution | `MetaTrader5` Python package (pip) + MT5 terminal on a demo account | Free. **The package runs on Windows only** → deploy on a Windows VPS or Windows PC. Build the rest platform-independent behind an interface so it can be unit-tested on any OS with a mock. |
| Economic calendar | Primary: **FinanceCalendar.com forex calendar API** (free with attribution, licensed for redistribution). Fallback: public Forex Factory weekly JSON feed (personal use only; respect their terms). Optional: Eulerpool free tier. | Implement a `CalendarProvider` interface so providers are swappable. Cache results in SQLite; refresh every 15 min, and every 1 min within ±10 min of a high-impact event. |
| News headlines | **Finnhub** free tier (`/news?category=forex` and `general`) + free RSS feeds (e.g. central-bank press releases: Federal Reserve, ECB; Reuters/FXStreet/Kitco RSS where available) | Free tier ≈60 calls/min. De-duplicate headlines by hash. |
| Macro context (optional) | **FRED API** (free) for US 10Y yield, real yields, DXY proxy | Daily context only. |
| AI news analysis | **Claude Haiku 5.5** via Anthropic API (`claude-haiku-5-5`) | Very cheap; keep prompts short (< 100K tokens, ideally < 2K). Use prompt caching for the fixed system prompt. Make the model configurable via `.env` (`LLM_MODEL`). Must work with the LLM disabled (rule-based fallback). |
| Alerts | Telegram Bot API (free) via `python-telegram-bot` or plain `httpx` | |
| Storage | SQLite (`sqlmodel` or `sqlite3`) | |
| Dashboard | Streamlit + Plotly candlestick charts | |
| Scheduling | `APScheduler` | |
| Data | `pandas`, `numpy` | |
| Config | `pydantic-settings` + `config.yaml` | |
| Tests | `pytest`, `pytest-mock` | |

## 3. Project structure

```
goldsweep/
  config.yaml            # strategy + risk params (editable)
  .env.example           # API keys, MT5 login, MODE=PAPER
  main.py                # entrypoint: scheduler + event loop
  goldsweep/
    core/settings.py      # pydantic settings, loads .env + config.yaml
    core/clock.py         # UTC clock, session calculator (zoneinfo)
    core/logging.py
    data/mt5_client.py    # connect, symbol resolve, rates, ticks, spread
    data/mock_broker.py   # offline mock implementing same interface
    data/calendar.py      # CalendarProvider interface + implementations
    data/news.py          # Finnhub + RSS fetchers, dedupe
    ai/news_analyzer.py   # LLM classification + rule-based fallback
    ai/scenarios.py       # pre-session scenario builder
    analysis/liquidity.py # liquidity pools
    analysis/structure.py # swings, BOS/MSS, displacement
    analysis/zones.py     # FVG + order blocks
    analysis/signals.py   # sweep → MSS → entry zone → signal
    risk/risk_manager.py  # sizing, limits, blackout, kill switch
    execution/executor.py # paper + live MT5 execution, trade management
    alerts/telegram.py
    storage/db.py         # SQLite models: candles, news, events, signals, trades
    backtest/engine.py    # event-driven backtester
    backtest/report.py    # metrics + HTML report
    dashboard/app.py      # Streamlit
  tests/
  DECISIONS.md
  README.md
```

## 4. Sessions and kill zones (XAUUSD)

Compute each session from its **local** time zone, then convert to UTC daily:

| Session | Local window | Zone | Purpose |
|---|---|---|---|
| Asia / Tokyo | 09:00–15:00 | Asia/Tokyo | Build the **Asian range** (main liquidity pool for London). Trading in Asia: off by default, configurable (gold often ranges here). |
| London kill zone | 07:00–10:00 | Europe/London | Primary: sweep of Asian high/low → reversal. |
| New York kill zone | 08:00–11:00 | America/New_York | Primary: sweep of London high/low or post-news sweep. |
| London–NY overlap | overlap of London 08:00–16:30 and NY 08:00–17:00 | both | Highest liquidity; allowed. |
| Avoid | 16:45–18:15 New York time (daily rollover / wide spreads) and Friday after 12:00 NY | America/New_York | No new trades. |

All windows live in `config.yaml` so I can edit them. Weekends and broker holidays: no trading (read from the calendar + `symbol_info` trade mode).

## 5. Strategy logic (the "best entry after liquidation" engine)

Implement as pure functions on pandas DataFrames (so they work identically live and in backtests). Timeframes: **H1** for bias, **M15** for liquidity, **M5** (or M1) for the trigger. All thresholds configurable.

**5.1 Higher-timeframe bias (H1)**
- Swing structure (higher highs/higher lows vs lower lows/lower highs) using a fractal swing detector (`swing_lookback=3`).
- Price relative to the previous day's midpoint (premium vs discount).
- News bias from 5.6 (score −1…+1).
- Combine into `bias ∈ {LONG, SHORT, NEUTRAL}`; in NEUTRAL, allow both sides but require a higher signal score.

**5.2 Liquidity pools (M15)**: Asian range high/low; previous day high/low (PDH/PDL); previous week high/low; current session high/low; **equal highs/lows** (two or more swing points within `eq_tolerance = 0.15 × ATR(14)`); round numbers (every $10 on gold, e.g. 2650, 2660). Store each pool with its type, price, creation time and `swept` flag.

**5.3 Sweep detection (M5)**
- A **sweep** = price trades beyond a pool by at least `min_sweep = 0.10 × ATR(14, M15)` and then a candle **closes back inside** within `max_bars_back_inside = 3` bars.
- Record the sweep extreme (wick high/low).

**5.4 Market-structure shift (MSS) + displacement**
- After a sweep of a **low**: a bullish MSS = close above the most recent M5 swing high formed after the sweep began, with a **displacement** candle (body ≥ `1.2 × ATR(14, M5)` or ≥ 70% of its range). Mirror for a sweep of a high.
- Must occur within `mss_window = 12` M5 bars of the sweep.

**5.5 Entry zone**
- Preferred: the **fair value gap (FVG)** created by the displacement (3-candle imbalance). Entry = limit order at the FVG midpoint (configurable: top/mid/bottom).
- Fallback: the **order block** = last opposite-colored candle before the displacement.
- Optional confluence: Fibonacci 0.62–0.79 retracement of the MSS leg (adds score, not required).
- **Stop-loss** = beyond the sweep extreme + buffer (`sl_buffer = 0.10 × ATR(14, M5)` + current spread).
- **Take-profit**: TP1 = nearest opposing liquidity pool; TP2 = next pool. Reject the setup if R:R to TP1 < `min_rr = 2.0`.
- Pending order expires after `entry_expiry = 6` M5 bars or if price hits TP1 first.

**5.6 News and scenario layer**
- **Calendar filter**: high-impact USD events (CPI, NFP, FOMC rate decision/minutes/presser, PCE, GDP, ISM, retail sales, jobless claims, Fed chair speeches) → **no new entries from 30 min before to 15 min after** (configurable); open trades: optionally move SL to breakeven if in profit ≥ 1R.
- **Post-news sweep mode**: after a high-impact release, the first spike often sweeps a nearby pool. Allow signals from 15 to 90 min after the release if 5.3–5.5 conditions are met (configurable on/off).
- **Headline classifier** (`ai/news_analyzer.py`): for each new headline, call the LLM with a short cached system prompt and get **strict JSON**:
  ```json
  {"relevant_to_gold": true, "direction": "bullish|bearish|neutral",
   "impact": "high|medium|low", "drivers": ["usd","real_yields","risk_off","geopolitics","central_bank_buying","inflation"],
   "confidence": 0.0, "one_line_reason": "..."}
  ```
  Validate with pydantic; on invalid JSON retry once, then fall back to a keyword rule set (e.g. "rate cut", "dovish", "escalation", "safe haven" → bullish; "hawkish", "strong dollar", "yields surge", "ceasefire" → bearish). Time-decay the news score (half-life 2h) and aggregate into `news_bias ∈ [−1, 1]`.
  - Gold-specific logic to encode in the prompt: gold usually moves **inversely to USD and US real yields**, rises on risk-off/geopolitical stress and on rate-cut expectations, and falls on hawkish Fed surprises.
- **Scenario builder** (`ai/scenarios.py`): 30 min before London and NY kill zones, and 60 min before each high-impact event, generate 2–3 short scenarios ("Bullish case / Bearish case / No-trade case") using: today's events with forecast vs previous, current pools and their distance in $ and ATR, H1 bias, news_bias. Use the LLM if enabled, otherwise a template. Send to Telegram and store.
- **The LLM never places trades directly.** It only produces a bias score and text; the deterministic engine decides.

**5.7 Signal scoring**: score 0–100 from components: sweep quality, displacement strength, FVG present, Fib confluence, agreement with H1 bias, agreement with news_bias, inside kill zone, spread normal. `alert_threshold = 60`, `auto_trade_threshold = 75` (configurable).

## 6. Risk management (enforced in `risk_manager.py`, cannot be bypassed)

- Risk per trade: `0.5%` of equity (max allowed setting 2%). Lot size = risk $ / (SL distance × contract size), rounded down to `volume_step`, clamped to `volume_min/volume_max` from `symbol_info`. **Read contract size, digits, point and tick value from MT5** — never assume (brokers differ: 100 oz contracts, symbol may be `XAUUSD`, `XAUUSD.m`, `GOLD`, etc.; auto-resolve the symbol).
- Max open positions: 1. Max trades per day: 3. Max trades per session: 2.
- **Daily loss limit**: −2% → stop trading until next day. **Weekly loss limit**: −5%. **Max consecutive losses**: 3 → pause 24h.
- **Spread filter**: skip if spread > `max_spread_points` (default: 2× the 20-day median spread, which the app measures itself).
- Slippage/deviation limit on market orders.
- Trade management: at +1R move SL to breakeven; close 50% at TP1, trail the rest behind M5 swing points to TP2 (configurable).
- **Kill switch**: Telegram command `/stop` and a dashboard button close all bot positions and disable trading; `/status`, `/pause`, `/resume` commands too (only from my Telegram chat ID).
- Prop-firm mode (optional): settings for max daily drawdown and max total drawdown that hard-stop the bot before the firm's limits.

## 7. Execution

- `executor.py` with `PaperExecutor` and `MT5Executor` sharing one interface.
- MT5: `initialize()` → `login()` → `symbol_select()`; use `order_check()` before `order_send()`; detect the allowed filling mode (FOK/IOC/RETURN) from `symbol_info`; set a unique `magic` number and a comment with the signal ID; handle all `retcode`s, requotes and reconnects with exponential backoff.
- Reconcile on startup: load open positions with our magic number and resume managing them.
- Every order, modification and close is written to SQLite and sent to Telegram.

## 7b. Auto-trade trigger + on-chart trade box (signal → order, no human click)

The live loop must work like this, fully automatic when `MODE=LIVE` and `auto_trade=true`:

1. **Trigger**: on every closed M5 bar (subscribe via a polling loop on `copy_rates_from_pos`, max 1s lag after bar close), run the signal engine. When a signal scores ≥ `auto_trade_threshold` and all risk checks pass, the executor sends the order immediately — no confirmation needed. Below the threshold but ≥ `alert_threshold`, only alert.
2. **Order type**: `entry_mode: market` (enter at close of the MSS candle) or `limit` (pending order at FVG). Both carry SL and TP in the same request.
3. **Multi-target split**: split the calculated total risk into N child positions (default 3) with the same entry and SL and different TPs: TP1 (nearest liquidity, 50% of volume), TP2 (next pool, 30%), TP3 (`3R` or session extreme, 20%). When TP1 fills, move the SL of the remaining positions to breakeven. Total risk across all child positions must never exceed `risk_per_trade_pct`.
4. **Optional scale-in (off by default)**: one add-on entry only if the first position is at ≥ +1R and its SL is already at breakeven, and the add-on's own risk keeps total open risk ≤ `risk_per_trade_pct`. Never add to a losing position.
5. **On-chart box in MT5**: write a small MQL5 indicator `GoldSweepPanel.mq5` that reads `signals.json` (written by Python into the MT5 `Common\Files` folder) and draws, for each signal: a SELL/BUY label, the entry line, SL line, TP1/TP2/TP3 dashed lines, a shaded risk/reward box, and the swept liquidity level. Python and MQL5 communicate only through this file (read-only for the indicator).
6. **Mobile monitoring**: positions are visible in the official MT5 mobile app (same login) automatically; also send Telegram messages on open, TP hit, SL hit and breakeven moves. Document clearly in the README that the bot runs on the Windows PC/VPS, not on the phone.
7. **Safety**: a duplicate-signal guard (one trade per sweep), a max-lots cap (`max_total_lots`), and a margin check refusing any order that would push margin level below 1000%.

## 8. Alerts format (Telegram)

```
🟡 XAUUSD LONG setup (score 81) — London kill zone
Swept: Asian low 2651.20 → MSS confirmed (M5)
Entry: 2654.80 (FVG mid, limit) | SL: 2649.90 | TP1: 2664.50 | TP2: 2671.00
R:R 2.0 / 3.3 | Risk 0.5% (0.12 lots)
News bias: +0.4 (dovish Fed speaker 40m ago) | Next high-impact: US CPI in 3h10m
Mode: PAPER  |  id: 2026-11-16-LDN-01
```
Plus: scenario briefs, pre-news warnings (15 min before), trade opened/closed with P/L in $ and R, and a daily summary at 21:00 New York time.

## 9. Build phases (stop after each for my approval)

1. **Foundation**: settings, clock/sessions (with DST tests for March/October/November transitions), logging, SQLite, mock broker, MT5 client (symbol resolve, rates, ticks, spread). CLI: `python main.py --check` prints account info, the resolved symbol and today's session times in UTC and Africa/Kampala time.
2. **Analysis engine**: swings, ATR, liquidity pools, sweeps, MSS, FVG/OB, signal builder + scoring. Unit tests using small hand-made candle fixtures for each pattern (sweep yes/no, MSS yes/no, FVG present/absent).
3. **Backtester**: event-driven, bar-by-bar (no look-ahead: only closed bars visible), with spread, commission and slippage modelling, session and news filters (use historical calendar data where available; otherwise flag it as a limitation). Download M1/M5 history from MT5 (as much as the broker provides). Output: trades CSV, equity curve, win rate, profit factor, expectancy (R), max drawdown, average R:R, results by session and by weekday. **Walk-forward test**: optimize on rolling 6 months, test on the next 2 months; report out-of-sample results separately. Warn loudly if parameters look overfitted.
4. **News + AI**: calendar providers, Finnhub/RSS news, LLM classifier with fallback, scenario builder, news blackout. Log LLM token usage and estimated cost per day.
5. **Alerts + paper trading**: Telegram alerts and commands, PaperExecutor running live on the demo data feed via APScheduler (on each M5 close + news events).
6. **Live execution (demo account only)**: MT5Executor, the automatic trigger loop and multi-TP split from 7b, `GoldSweepPanel.mq5` on-chart box, trade management, reconciliation, kill switch.
7. **Dashboard**: Streamlit — live chart with pools/FVGs/signals drawn, today's scenarios, news feed with AI tags, open trades, risk status, equity curve, backtest reports.
8. **Hardening + deployment**: Windows VPS setup guide (MT5 auto-login, run as a service with NSSM or Task Scheduler, auto-restart, daily log rotation, heartbeat message to Telegram every hour; alert if no heartbeat), README with a full setup walkthrough.

## 10. Acceptance criteria

- `pytest` passes; core analysis modules ≥ 80% coverage.
- Backtest report is reproducible from one command: `python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31`.
- Paper mode runs 24/5 without crashing; restarts recover state.
- No order is ever sent without SL and TP (add a test that asserts this).
- Risk limits are enforced in tests (daily loss, max trades, spread filter, news blackout).
- The app runs fully with the LLM disabled.

## 11. `config.yaml` starter (create with these defaults)

```yaml
symbol_candidates: [XAUUSD, XAUUSD.m, XAUUSDm, GOLD]
mode: PAPER
timeframes: {bias: H1, liquidity: M15, trigger: M5}
sessions:
  asia:   {tz: Asia/Tokyo,        start: "09:00", end: "15:00", trade: false}
  london: {tz: Europe/London,     start: "07:00", end: "10:00", trade: true}
  newyork:{tz: America/New_York,  start: "08:00", end: "11:00", trade: true}
  rollover_block: {tz: America/New_York, start: "16:45", end: "18:15"}
strategy:
  atr_period: 14
  swing_lookback: 3
  eq_tolerance_atr: 0.15
  min_sweep_atr: 0.10
  max_bars_back_inside: 3
  mss_window: 12
  displacement_atr: 1.2
  entry_mode: fvg_mid
  sl_buffer_atr: 0.10
  min_rr: 2.0
  entry_expiry_bars: 6
  alert_threshold: 60
  auto_trade_threshold: 75
news:
  blackout_before_min: 30
  blackout_after_min: 15
  post_news_sweep_mode: true
  headline_half_life_min: 120
  llm_enabled: true
risk:
  risk_per_trade_pct: 0.5
  max_open_positions: 1
  max_trades_per_day: 3
  daily_loss_limit_pct: 2.0
  weekly_loss_limit_pct: 5.0
  max_consecutive_losses: 3
  breakeven_at_r: 1.0
  partial_close_at_tp1_pct: 50
display_timezone: Africa/Kampala
```

## 12. `.env.example`

```
MODE=PAPER
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
ANTHROPIC_API_KEY=
LLM_MODEL=claude-haiku-5-5
FINNHUB_API_KEY=
FRED_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```
