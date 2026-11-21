# Scalper add-on: S0 review and plan

The owner's prompt is `docs/SCALPER_SPEC.md`; the owner added "add this alongside
the trade system". This is phase S0: what the existing code already provides, what
is new, the files, the conflicts (recorded as D88-D99 in `DECISIONS.md`) and the
revised phases. Nothing is built yet. Written 2026-11-21.

## The add-on in one paragraph

A second strategy on the same account and in the same bot: an M1 scalper that
trades sweeps of small liquidity (recent swing points, twin bars, session edges)
with a step-trail line as trend and stop, one signal at a time split into three
child positions (TP1/TP2/TP3 at 0.5/1.0/1.5 R), an adaptive trail, a time stop and
its own risk limits. Around it: an on-chart dashboard and trade boxes in
GoldSweep's own look, an assistant panel inside MT5 backed by a local service,
and Telegram. The current sweep + MSS strategy keeps running unchanged.

## What the review found

1. **The prompt assumes a "Scalping Mode, section 7c", a `scalping:` config block,
   `GoldScalper.mq5` and a `scalp_forward_test_ok.json` lock.** None exist here: the
   spec (`docs/SPEC.md`) stops at 7b and no scalping code was ever built. They are
   created by this add-on (D91, D93).
2. **The prompt puts the strategy and the orders in an MQL5 expert advisor.** This
   project sends every order from Python through `execution/mt5_gateway.py`, with
   one `TradeBook` for backtests, paper and live, the unattended-start lock
   (`/golive`) and one kill switch (CLAUDE.md). A trading EA would bypass all of
   them and need a second copy of every rule. Recommended instead: Python decides
   and trades, MQL5 draws and has buttons (D89). **This is the decision to make at
   this stop.**
3. **The live executor closes any position with GoldSweep's magic number that is
   not in its saved state.** The scalper must have its own magic number, its own
   book and its own state, or the main system would close its trades (D88).
4. **Each part of a main trade already goes to the broker with its own hard TP**
   (TP1, TP2, TP3), so the exits happen even if the PC or the bot stops. The prompt's
   "virtual" TP1/TP2 (all children with TP3 at the broker, closed early by the EA)
   depend on the bot being alive. The scalper uses the broker-side way (D90).
5. **One account, two strategies.** Today's risk limits belong to one book. Both
   strategies need account-wide limits (daily and weekly loss, combined open risk)
   and a rule against opposite positions, which would be hedging (D91, D92).
6. **Scalping costs.** On M1 the stop is a few dollars, so the spread, commission and
   slippage are a large share of TP1 at 0.5 R; with the stop at breakeven after TP1 the
   strategy needs a high win rate. Whether it is positive after costs is unknown. The
   first backtest therefore moves into S1, so the owner can stop early if it is not
   (D95).
7. **Telegram volume.** Several scalps an hour, with a message for every child, TP
   and trail step, would be dozens of messages a day; the owner has just chosen fewer
   messages (D87). Quieter defaults (D97).
8. **Two of the four strategy modes have no rules in the prompt** (`momentum_burst`,
   `asia_reversion`). Only `sweep_reclaim` and `trail_rejection` are built (D94).
9. **The reference material is a third-party commercial product's.** It stays out of
   the repository; the videos could not be played here, so the owner's written
   analysis of them and the three screenshots were used (D98).

## Reused and new

| Needed | Reused from GoldSweep | New for the scalper |
|---|---|---|
| Sessions, rollover block, Friday cutoff, weekend | `core/clock.py` `SessionCalculator` | which windows the scalper trades (config) |
| News blackout, post-news windows | `data/calendar.py`, `entry_deadline` | - |
| Higher-timeframe bias | `analysis/bias.py` (H1) | used as the scalp side filter (no JSON bridge needed inside one process) |
| Session highs/lows, Asian range | `analysis/liquidity.py` | micro levels: last swing points, twin bars |
| ATR | `analysis/indicators.py` | - |
| Step-trail line | - | `scalping/trail.py` |
| Sweep "X", twin bars, signal rules | sweep idea of `analysis/` | `scalping/levels.py`, `scalping/engine.py` (M1, closed bars only) |
| SL, TP ladder, cost gate | `TradePlan` (needs SL and TP to exist) | ladder in `scalping/plan.py` |
| Child split | `plan_legs` (50/30/20 today) | takes a split list (40/30/30 for scalps) |
| Breakeven after TP1, never loosen a stop | `breakeven_due`, `TradeBook` | stop to TP1 after TP2, step-trail rule, time stop: added to `execution/book.py` as shared rules |
| Position sizing, spread filter, daily limits | `risk/risk_manager.py` | a second instance with scalping limits, plus `risk/account_guard.py` (both strategies) |
| Paper trading | `PaperExecutor` | second instance, own state and table |
| Orders on MT5 | `MT5Gateway`, `MT5Executor` | second instance with its own magic number |
| Backtests | `Simulator`, metrics, report, walk-forward | M1 signal pass, tick loader and replay, scalp report sections |
| Kill switch, `/golive` lock | `live.py` | also covers the scalp executor |
| Telegram | notifier, commands | `/scalp on|off`, `/trail on|off`, `/stats` |
| Dashboard | Streamlit app (reads SQLite) | a Scalper tab |
| LLM | `ai/llm.py` `LlmChat.complete()` | assistant free text |
| On-chart drawing | `mt5/GoldSweepPanel.mq5` (reads `signals.json`) | v2: tags, step line, X, twin bars, boxes, dashboard, buttons |
| Assistant | - | `assistant/` service, `mt5/GoldSweepAssistant.mq5` |

## Files

New:

```
goldsweep/scalping/__init__.py
goldsweep/scalping/trail.py          step-trail line (pure)
goldsweep/scalping/levels.py         micro levels, sweep X, twin bars (pure)
goldsweep/scalping/engine.py         ScalpEngine: one closed M1 bar at a time -> signals
goldsweep/scalping/plan.py           SL, TP ladder, cost gate, child split
goldsweep/scalping/backtest.py       M1 signal pass, tick replay, report  (python -m goldsweep.scalping.backtest)
goldsweep/scalping/ticks.py          tick download and storage
goldsweep/risk/account_guard.py      limits across both strategies
goldsweep/execution/scalper.py       the M1 job: signal -> checks -> scalp executor
goldsweep/assistant/__init__.py, server.py, quick.py
mt5/GoldSweepAssistant.mq5
tests/test_scalp_trail.py, test_scalp_levels.py, test_scalp_engine.py, test_scalp_plan.py,
tests/test_scalp_execution.py, test_account_guard.py, test_assistant.py, test_branding.py
docs/SCALPER_SPEC.md, docs/SCALPER_PLAN.md
```

Changed: `core/settings.py` and `config.yaml` (one `scalper:` block, D91),
`storage/db.py` (migrations: scalp trades, scalp signals, assistant history),
`execution/book.py` (split list, new shared stop rules), `live.py` (M1 job, kill
switch, `/status`), `alerts/messages.py`, `alerts/telegram.py`, `dashboard/`,
`mt5/GoldSweepPanel.mq5` (v2), `main.py` (`--assistant`), `requirements.txt`
(fastapi, uvicorn), `.gitignore` (`reference/`), README, CLAUDE.md, DECISIONS.md.

Not built: `GoldScalper.mq5` as a trading EA and the Python/MQL5 parity test, if
the owner accepts D89 (one engine needs no parity test).

## Phases (revised; stop after each for approval)

- **S1 - Logic in Python, and a first verdict.** Step trail, micro levels, sweep X,
  twin bars, `sweep_reclaim` and `trail_rejection`, SL and TP ladder with the cost
  gate, the 40/30/30 split, the adaptive-trail rules. Tests on hand-made bars,
  including the video-2 trade (entry 4058.34, SL 4067.64, TP1 4053.69, TP2 4049.05,
  TP3 4044.40, a stop that only tightens). Then a first backtest on the stored
  2024-2026 M1 bars with costs (worst case inside a bar): trades per day, win rate
  against the breakeven win rate, R per trade gross and net. If it loses after
  costs, the owner decides whether to continue.
- **S2 - Running alongside the main system.** The scalp executor (paper, and MT5
  with its own magic number), the M1 job in the bot, the account guard, the
  no-hedging rule, the lock (D93), the kill switch and `/golive` rules, the
  `scalp.json` file for the chart and a command file from its buttons.
- **S3 - Visuals.** `GoldSweepPanel.mq5` v2 in GoldSweep's look: signal tags, step
  line, X markers, twin-bar brackets, trend lines, trade boxes with TP-hit marks and
  the result, the three-column dashboard with Adaptive Trail and Pause buttons.
  Screenshots from the demo chart and from a replay.
- **S4 - Assistant.** The MT5 panel and the local service: quick commands answered
  from the bot's saved state (no AI, timed), Get Chart Data, free text through the
  existing LLM layer, history and search, WebRequest with a file fallback.
- **S5 - Telegram and mobile.** Messages (quiet defaults), chart picture, commands;
  `GS-SC|<id>|<n>` comments so the phone app shows which trade a position belongs to.
- **S6 - Testing and tuning.** Tick replay where the broker has tick history,
  walk-forward, the `tight` (0.5 R steps) and `wide` (2.0 R) presets, results by
  mode, session and spread; then at least 4 weeks of forward testing on the demo
  account and a backtest-against-live comparison.

## For the owner at this stop

1. **Where the scalper's decisions and orders live (D89).** Recommended: Python, as
   everything else, with MQL5 for drawing and buttons. The prompt's way: an MQL5 EA
   that trades, mirrored in Python.
2. The safer defaults recorded in D88-D99 (account-wide limits, the main strategy's
   priority, starting numbers, quiet Telegram) can be changed at any stop.
