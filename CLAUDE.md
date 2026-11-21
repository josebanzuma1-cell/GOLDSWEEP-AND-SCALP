# GoldSweep: notes for Claude Code

XAUUSD-only trading assistant. The full specification is `docs/SPEC.md`
(the owner's build prompt, verbatim). Choices made where it was open are in
`DECISIONS.md`. The phase table in `README.md` shows what exists.

## How the work is organised

- Build one phase of `docs/SPEC.md` section 9 at a time. At the end of a phase:
  run the tests, show the results, summarise what was built, then **stop and
  wait for the owner's approval** before starting the next phase.
- When a requirement is ambiguous, take the safer option and record it in
  `DECISIONS.md`.

## Rules that are never relaxed

1. Never claim "no losses", "guaranteed profit" or a fixed win rate, in code,
   comments, UI text or docs. The goal is small, controlled losses and positive
   expectancy.
2. Every order carries a hard stop-loss and take-profit when it is sent. No
   martingale, grid, averaging down or hedging to recover losses.
3. PAPER is the default. LIVE needs `MODE=LIVE` in `.env`, `mode: LIVE` in
   `config.yaml` and a typed confirmation at startup. Build and test on a demo
   MT5 account.
4. All times are timezone-aware UTC internally. Sessions are defined in local
   time and converted with `zoneinfo`; never hard-code a UTC offset.
5. Secrets live only in `.env`, which is never committed.
6. Type hints, docstrings and `pytest` tests for every module. Log through
   `goldsweep.core.logging.get_logger`.
7. The LLM (Phase 4) never places trades; it only produces a bias score and text.

## Conventions already in the code

- **Candle frames** (`goldsweep/data/candles.py`): index is the bar open time,
  UTC, named `time`; columns `open, high, low, close, tick_volume, spread`.
- **Closed bars only.** Brokers return only finished bars unless
  `include_forming=True`. Strategy code must never see a forming bar.
- **Broker timestamps are true UTC** once they leave the data layer. The MT5
  client converts from server time (`goldsweep/core/server_time.py`).
- **Instrument facts come from the broker** (`SymbolSpec`): never assume
  contract size, digits, point or tick value.
- **Time is injected.** Take a `Clock` instead of calling `datetime.now()`, so
  tests and backtests control time.
- **Config is strict.** New settings need a field in `goldsweep/core/settings.py`
  and a line in `config.yaml`; unknown keys are rejected.
- **Schema changes** are new entries in `MIGRATIONS` in `goldsweep/storage/db.py`.
- **Analysis** (`goldsweep/analysis/`) is pure functions on candle frames, plus
  one stateful `SignalEngine` fed one closed M5 bar at a time. A swing exists only
  once its confirming bars have closed, a gap only once its third candle has.
  The trading day is 17:00-17:00 New York. Engine tests replay hand-made bars
  (`tests/test_signals.py`); fixture rows are `(open, high, low, close)`.
- **"CDT" in the owner's Phase 2 request means CRT** (Candle Range Theory),
  confirmed by the owner (DECISIONS.md D16).
- **Backtests** (`goldsweep/backtest/`) replay bars through the same `SignalEngine`
  once (cached in `data/backtest-cache/`), then `Simulator` plays the signals under
  `goldsweep/risk/risk_manager.py`. Bars are bid prices; when the order inside a
  bar is unknown, assume the worse case. Report files must stay reproducible: no
  run timestamps in them.
- **The pressure meter** (`analysis/pressure.py`) is an estimate from tick volume,
  candles, structure and news. Never call it real order flow; it does not score.
- **Network reads** of calendars and feeds go through a `Fetcher`
  (`data/http.py`); tests pass fakes and never touch the network. Keys go in
  headers, never in URLs.
- **Model calls** go through `LlmChat.complete()` in `ai/llm.py` (Claude, Groq or
  any OpenAI-style service, chosen by `LLM_PROVIDER`), so every call lands in the
  `llm_usage` table and counts against the daily budget and the call caps. Tests
  use fakes (`FakeClient`, `FakePost`): never call the real API from tests. Do not
  send `temperature` (Claude Haiku 5.5 rejects it). The model classifies and
  writes text only; it never places a trade.
- **Trades** are managed by one `TradeBook` (`execution/book.py`) for backtests
  and paper trading; the live executor (`execution/live_executor.py`) uses the same
  rule functions (`plan_legs`, `breakeven_due`, `trailed_stop`). Change trade
  behaviour there, never in only one mode. A `TradePlan` without a valid SL and TP
  cannot be created, and `MT5Gateway.open` refuses an order without both; keep it so.
- **Waiting entries never wait into a blocked time.** Every new plan goes through
  `TradePlan.with_deadline` (`entry_deadline` in `execution/book.py`) before it is
  submitted, in the simulator and in `live.py`, so its expiry stops before the next
  news blackout, rollover block, Friday cutoff or weekend. `entry_expiry_bars` is not
  part of the signal cache key: the simulator applies it (D82).
- **M1 refinement** (`analysis/refine.py`, D83) is a pure function of a saved
  `RefineSetup` and the closed M1 bars; live trading re-runs it each minute
  (`Trader.refine_job`), the simulator once per setup. Refined orders are built with
  `refined_plan` and go through the same deadline rule, risk checks and executor.
  `strategy.refinement` is not part of the signal cache key.
- **Exit targets** come from `TradePlan.with_exit_targets(risk)` (pools or fixed R,
  D84), applied wherever a plan is made (simulator, `live.py`, refined plans), before
  `with_deadline`. Reaching TP1 always moves the stop to breakeven (the owner's
  rule); `breakeven_at_r: 0` only turns off the move by distance.
- **Setup models** (D85, D86): `analysis/signals.py::MODELS` (sweep_mss, cisd,
  continuation, breakout), each run as its own `SignalEngine(model=...)`.
  `config.yaml models:` says which are enabled and which may trade; the rest alert
  only. Backtests generate signals for the trading models only, each with its own
  cache entry. Duplicate checks in `live.py` are per model. Confluence tags
  (`Signal.tags`: unicorn, bpr, judas, silver_bullet, ote, smt) are recorded and
  reported, never scored; SMT is added after the engine (`analysis/smt.py`) from
  silver bars. Tests that follow the spec's model end to end use
  `tests.helpers.only_spec_model`.
- **Session playbooks** (D86): `config.yaml playbooks:` gives each session (asia,
  london, newyork; the overlap and post-news count as newyork) a `trading` rule and an
  A+ `candidate` rule (`PlaybookRule`: model, min_score, tags_any, min_tags counting
  confluence tags only, tags_all). `analysis/grades.py` grades A+/A/B; the context
  tags (`analysis/context.py`: d1_trend, h4_trend, ema_trend, weekly_value,
  high_vol) are added after the engine like SMT, from H1 bars anchored to the 17:00
  New York day. `python -m goldsweep.playbook` picks candidates: chosen on the first
  two-thirds, positive on the held-out third, at least 15 trades. A candidate is
  monitored before it may trade; never switch one to trading without the owner.
- **Only `execution/mt5_gateway.py` sends orders.** Tests drive it with
  `tests/fake_mt5_trading.py` (a fake trade server); never send orders from tests.
  Real-terminal checks use `order_check` only, which validates without trading.
  When calling the real MetaTrader5 package, never pass an empty `**kwargs`
  (order_check/order_send then fail with "Unnamed arguments not allowed").
- **The dashboard** (`goldsweep/dashboard/`) only reads SQLite. The bot writes
  what it needs (`dashboard.snapshot`, `dashboard.heartbeat`, M5 candles); the
  Control tab writes `commands` rows that the bot's `dashboard_job` carries out.
  Never connect the dashboard to MT5 or let it send orders. Page tests use
  Streamlit's `AppTest` with `GOLDSWEEP_DASHBOARD_DB` pointing at a test database.
- **Unattended starts are unarmed** (`--live --unattended`, Task Scheduler): the
  executor's `armed` is False until the owner sends `/golive <account>`; while
  unarmed nothing may send an order except the kill switch. Keep it that way for
  any new order-sending code.
- **The Anthropic SDK** is imported lazily (`ai/llm.py`): importing it costs over ten
  seconds on this laptop. Do not add module-level `import anthropic` in app code.

## Commands

```
python -m pytest                 run the tests
python -m pytest --cov           with coverage
python main.py --check --mock    environment report, offline
python main.py --check           environment report against MetaTrader 5
python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31   backtest report
python -m goldsweep.backtest --from 2026-01-05 --to 2026-01-30 --mock   offline
python main.py --news [--no-llm] [--mock]   calendar, headlines, news bias, scenarios
python main.py --paper [--mock] [--no-llm]  paper trading loop (Ctrl+C stops it)
python main.py --live [--no-llm]            live trading (MODE=LIVE + mode: LIVE + typed confirmation)
python main.py --dashboard [--mock]         the Streamlit dashboard at http://localhost:8501
python main.py --live --unattended          scheduled start: trades after /golive on Telegram
python main.py --watchdog                   one check; Telegram alert if the bot is quiet
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 [-Paper] [-DryRun]
```

Run them from the project's virtual environment (`.venv`).
