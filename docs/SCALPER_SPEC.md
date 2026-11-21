<!--
The owner's build prompt for the scalper add-on, received 2026-11-21, kept verbatim
below the line (as docs/SPEC.md keeps the main build prompt). The owner added: "ADD THIS
ALONGSIDE THE TRADE SYSTEM": the scalper runs next to the existing strategy, not
instead of it. Where this prompt conflicts with the project as built (it refers to a
"CLAUDE.md section 7c", a `scalping:` block and GoldScalper.mq5, none of which exist),
the plan in docs/SCALPER_PLAN.md and DECISIONS.md D88-D99 apply. The third-party
product named below is described only so that nothing of it is copied.
-->

# Claude Code Prompt — GoldSweep **Scalper Add-on** + Terminal UI Design

> How to use: copy this whole folder (`SCALPER_ADDON_PROMPT.md` + `reference/`) into the root of the existing GoldSweep project, then tell Claude Code:
> "Read CLAUDE.md, then read SCALPER_ADDON_PROMPT.md and look at every image in /reference. Start with Phase S0."

---

## 0. Context and ground rules

You are extending an **existing** project: GoldSweep, the XAUUSD news + liquidity-sweep alert and auto-trading system described in `CLAUDE.md` (sections 0–12, including 7b auto-trade trigger/on-chart box and 7c Scalping Mode). Read `CLAUDE.md`, `DECISIONS.md` and the current code first. **Do not break or rewrite working modules**; extend them. Reuse the existing risk manager, news filter, sessions, SQLite, Telegram alerts, kill switch and `config.yaml`.

All rules from `CLAUDE.md` section 0 still apply. Especially:
1. No claims of "no losses", "guaranteed" or fixed win rates anywhere (code, UI, docs).
2. Every order carries a hard SL and TP in the order request.
3. No martingale, no grid, no averaging down, no stacking positions beyond the risk cap.
4. `MODE=PAPER` by default; live scalping stays locked until `scalp_forward_test_ok.json` exists (section 7c of CLAUDE.md).

**Branding rule (important):** the reference images come from a third-party commercial product ("Prophet Ultra / Prophet Ultra Plus" by 1 Trick / Wantrick). Use them **only** to understand layout and behaviour. Do **not** copy its name, robot mascot, logo, "P" emblem, purple robot background, or any of its text. GoldSweep has its own identity (section 3).

## 1. What the reference material shows (my analysis — treat as the spec)

### Image `01_overview_M1_buy_box_dashboard.jpg` (MT5, XAUUSD, M1, live account, EA attached)
- Main chart on **M1** with many small **BUY / SELL tag labels** printed at signal candles throughout the session; signals happen often (scalping frequency: several per hour).
- A **stepped line** above/below price that only moves in the trend direction (a step-trailing / structure stop line, similar to a SuperTrend), switching sides when the trend flips.
- An active **BUY trade box** on the right: `SL 3764.49`, `ENTRY ~3768`, `TP1 3774.91`, `TP2 3781.78`, `TP3 3788.65`. The TPs are **equally spaced** (each step ≈ $6.87). Shaded box = reward zone, darker strip = risk zone, labels right-aligned at the box edge.
- **Info dashboard panel** in the bottom-left corner with a coloured header bar (product name + an **"Adaptive Trail"** toggle button on the right), a one-line status row, and **three columns**: (1) market state (trend, volatility/ATR, regime, session, spread); (2) trade status (TP1/TP2/TP3 hit flags, current stop type, trailing mode); (3) performance (win rate, profit factor, number of trades, P/L, stats mode).
- Volume histogram at the bottom of the chart. Algo Trading is on; status bar shows ping ~137 ms (too slow for real scalping; we target < 10–50 ms on a VPS).
- The account balance shown is tiny (~$44), which means very high relative risk per trade. **We must not imitate that.**

### Image `02_sell_box_TP_ladder_step_trail.jpg` (zoomed SELL setup)
- Stepped trailing line coming down from the top-left as price makes lower highs.
- An **"X" marker at a swing high** just before the entry (a liquidity grab / rejection point), then entry at `3877.57` with `SL 3887.78` (stop distance ≈ $10.21).
- TP ladder: `TP1 ≈ 3854.9`, `TP2 3832.24`, `TP3 3809.57` (equal steps ≈ $22.67 ≈ 2.2 × stop distance).
- A small "TP1" marker printed where TP1 was hit; price kept falling toward TP3; a horizontal red line shows the current price.

### Image `03_assistant_chat_panel_M5.jpg` (+ zoom `03b`)
- An **assistant chat panel inside MT5** (floating window over the chart, M5 XAUUSD) with: avatar, buttons **Search, New Chat, Clear, History**, a quick-command chip **"Twin Bars"**, a prompt box ("Type your prompt here… Enter for newline, Shift+Enter to send"), a **"Get Chart Data"** button with a dropdown, and a send arrow.
- Example exchange: user taps **"Quick Scalp (Manual)"** → reply:
  `Quick Scalp -> NONE` / `Strategy=XG Boost; status=READY, market side=BULLISH, signal=NONE` / `(Local calculation in 0 ms)`.
  So quick commands are answered **instantly by local rule-based logic** (no AI call), reporting the active strategy, readiness, market side (bias) and whether a signal exists right now. Free-text questions can go to an AI.

### Video 1 (`…16-05-15.mp4`, 10 s)
- A brand intro: a purple armoured robot mascot on a mountain top. **No trading content.** Not to be reproduced. No usable speech (I could not transcribe the audio; it appears to be music/branding).

### Video 2 (`…16-05-25.mp4`, 34 s) — frames `04_video2_trade_sequence_*` and `05_video2_positions_table_SL_trailing.jpg`
A full SELL scalp, start to finish, on XAUUSD:
- Signal: SELL tag after a drop, a diagonal red trend line from the swing high, a stepped trail line above price.
- Trade box: `ENTRY 4058.34`, initial `SL 4067.64` (≈ $9.30), `TP1 4053.69` (0.5 × stop), `TP2 4049.05` (1.0 × stop), `TP3 4044.40` (1.5 × stop).
- **Three positions** opened (entries 4058.34, 4058.34, 4058.81), all with comment = EA name, **all with the same broker TP (4044.78 ≈ TP3)** and the **same SL that moves together**. TP1/TP2 exits are done by the EA closing positions itself ("virtual" partial targets), while the broker-side TP/SL stay as the hard safety net.
- The SL steps down over time as the trail line moves: 4067.64 → 4066.15 → 4065.66 → 4064.91 → 4063.71 → 4060.53 → 4059.62 → 4059.30 (never moves backwards).
- Floating P/L went **negative first** (around −$13 to −$23 total) before price fell; ended with one position left showing about +$16 to +$28. Lesson: even good setups go into drawdown first, so the stop must be real and sized correctly.
- Timeframe/clock in the video: 3 Aug, ~02:38–04:14 server time (Asian session into early London).

### Video 3 (`…16-05-29.mp4`, 41 s) — frames `06_video3_mobile_stacked_positions_*`
- MT5 **mobile app** (Exness-style `XAUUSDm`, `BTCUSDm` symbols) showing the account the EA trades on: about **15+ XAUUSD sells of 0.10 lot** opened around 3948 (current ~3915, about +$320–330 each), about **12+ BTCUSD sells of 0.10** (~73,459 → 68,617, about +$480 each), plus several XAUUSD buys of 0.09. Balance ~$7,600, floating equity much higher.
- What it really shows: **many same-direction positions stacked on one account**. That worked in this clip, but ~1.5–2 lots of gold on a ~$7.6k balance loses about $150–200 for every $1 gold moves against it; a $40–50 adverse move would wipe the account. **GoldSweep must not stack like this.** We allow one trade split into up to 3 child positions, with total risk ≤ the per-trade risk cap.
- Also shows: the trades are fully visible on a phone because the EA runs on a PC/VPS on the same MT5 login. We replicate that, plus Telegram.

## 2. What to build (summary)

1. **GoldSweep Scalper v2 engine** (extends `GoldScalper.mq5` from CLAUDE.md 7c): M1/M5 signal tags, step-trail line, sweep "X" markers, 3-position TP ladder with virtual TP1/TP2 exits, adaptive trailing stop, all under the existing scalping risk limits.
2. **On-chart visual system** (`GoldSweepPanel.mq5` v2): trade box with SL/ENTRY/TP1–TP3 labels, TP-hit markers, signal tags, step-trail line, trend lines, info dashboard with an Adaptive Trail toggle — in GoldSweep's own visual identity.
3. **In-terminal assistant panel** (`GoldSweepAssistant.mq5`) talking to a local Python service: quick commands answered locally in milliseconds, free-text questions answered by the LLM, "Get Chart Data" summaries.
4. **Mobile + Telegram monitoring** with clear position comments.
5. Tests, Strategy Tester presets, a demo forward test, and documentation.

## 3. GoldSweep visual identity (original — use this, not the reference look)

- Theme: dark charcoal chart background `#0E1116`, grid off, candles: bull `#E9EEF2` (off-white body), bear `#C9A227` (gold body); wicks same colour.
- Accent colours: gold `#D4AF37` (brand), BUY green `#2ECC71`, SELL red `#E74C3C`, risk zone `rgba(231,76,60,0.18)`, reward zone `rgba(46,204,113,0.12)`, TP lines dashed `#9AD1A8`, SL line solid `#E74C3C`, step-trail line `#8FA3B8`.
- Font: Segoe UI / Consolas (MT5 safe). Brand mark: the text **"GoldSweep"** with a small gold diamond `◆` — no mascots, no background artwork (a clean chart is easier to read).
- Every visual element is configurable (colours, sizes, show/hide) via EA inputs and `config.yaml`.

## 4. Scalper v2 strategy logic

Implement the detection in MQL5 (`GoldScalper.mq5`) for speed, and **mirror the same logic in Python** (`goldsweep/scalping/logic.py`) for the tick replay backtester and unit tests. Both implementations must produce identical signals on the same data (add a parity test using exported bars).

### 4.1 Step-trail line (trend + stop engine) — the stepped line in images 01/02 and video 2
- ATR-based step trail on the trading timeframe (default M1, option M5): `basis = (high+low)/2`, `upper = basis + k × ATR(n)`, `lower = basis − k × ATR(n)` (defaults `n=10`, `k=2.0`).
- In an uptrend the line = max(previous line, lower); in a downtrend = min(previous line, upper); trend flips when a candle **closes** through the line. The line only moves in the trend's favour (this produces the stairs).
- Draw it as a stepped polyline (horizontal + vertical segments), colour per trend.
- Its value is also used by the Adaptive Trail (4.5).

### 4.2 Micro-liquidity and sweep marker ("X")
- Track the last `micro_lookback` (default 15) bar swing highs/lows plus session highs/lows and the Asian range edges (from the main engine).
- Sweep = a wick beyond a level by ≥ `sweep_min_atr × ATR(M1)` and a close back inside within 2 bars. Draw an **"X"** marker at the swept extreme.
- **"Twin Bars" detector** (quick command in the assistant): two consecutive candles with equal highs (tweezer top) or equal lows (tweezer bottom) within `0.05 × ATR`, i.e. a micro double-top/bottom = resting liquidity. Mark them with a thin bracket; a sweep of a twin-bar level scores higher.

### 4.3 Entry signal (BUY / SELL tag)
A **SELL** signal (mirror for BUY) when all are true on a closed bar:
1. Step-trail trend is down, or it flips down on this bar.
2. A sweep "X" of a micro high/twin-bar high happened within the last `sweep_window` bars (default 6), **or** price rejected the step-trail line (wick touched it, body closed below).
3. A bearish close below the low of the sweep candle (micro structure shift).
4. Spread, cost gate, news blackout, session and risk checks from CLAUDE.md 7c all pass.
5. Higher-timeframe bias from Python (`scalp_context.json`) is `short` or `both`.
- Print a **SELL tag** (rounded label, red border) above the signal candle; BUY tag (green) below it. Store each signal with an ID, score and reason.
- Strategy modes (selectable in the panel and config): `sweep_reclaim` (default), `trail_rejection`, `momentum_burst` (off by default), `asia_reversion` (off). Show the active one as `Strategy=<name>` in the assistant replies.

### 4.4 Trade box and TP ladder (images 01/02, video 2)
- `SL` = beyond the sweep extreme (or the step-trail line if it's closer than `sl_max_atr × ATR`) + buffer + spread. Reject if stop distance < `sl_min_atr × ATR` (too tight for costs) or > `sl_max_atr × ATR`.
- Ladder: `TPn = entry ∓ n × step`, where `step = tp_step_r × stop_distance` (default `tp_step_r = 0.5`, so TP1 = 0.5R, TP2 = 1.0R, TP3 = 1.5R, matching video 2; preset `wide` = 2.0R steps like image 02).
- **Cost gate still applies to TP1**: TP1 distance must be ≥ `cost_multiple × round_trip_cost`; if not, widen `tp_step_r` up to its maximum or skip the trade.
- Draw the box from the signal bar to `box_bars` bars ahead (default 40): risk zone between ENTRY and SL, reward zone between ENTRY and TP3, dashed TP1/TP2 lines, right-aligned labels `SL 0000.00`, `ENTRY 0000.00`, `TP1/TP2/TP3 0000.00`. When a TP is hit, print a small "TP1"/"TP2"/"TP3" marker at that bar. When the trade closes, freeze the box and show the result (+R / −R) in its label.

### 4.5 Execution: 3 child positions + virtual targets + Adaptive Trail
- On a signal, split the trade's total risk (`risk_per_scalp_pct`, default 0.25%) across **3 child positions** with volumes 40% / 30% / 30% (configurable), all at market (or limit at the reclaim level), each with the **same hard SL** and the **same broker TP = TP3**. Comment format: `GS-SC|<signalId>|<1/2/3>` (fits MT5's 31-char comment limit).
- Virtual exits managed by the EA: close child 1 at TP1, child 2 at TP2; child 3 runs to TP3 or the trail.
- **Adaptive Trail** (toggle button on the dashboard, input `adaptive_trail=true`):
  - After TP1: move SL of the remaining children to entry + costs (breakeven+).
  - After TP2: move SL to TP1.
  - Continuously: SL = the step-trail line ± buffer, but only if that improves the stop (never loosens it).
  - Modify all remaining children together (one `OrderSend` per position, `TRADE_ACTION_SLTP`) and respect the broker's freeze/stops level.
- Time stop (from 7c): if not at ≥ +0.3R after `time_stop_sec`, close all children.
- Hard limits: total open risk of all children ≤ `risk_per_scalp_pct`; one active scalp signal at a time; no new signal in the same direction for `cooldown_sec` after a stop-out; **never open additional same-direction positions outside the 3 children** (explicitly prevents the stacking seen in video 3).

### 4.6 Multi-symbol (prepare, keep disabled)
Video 3 shows BTCUSD as well. Make symbol handling generic (symbol resolver, per-symbol ATR, contract size, cost settings) but keep `scalping.symbols: [XAUUSD]` only. BTCUSD can be enabled later after its own backtest.

## 5. Info dashboard (`GoldSweepPanel.mq5` v2) — bottom-left of chart

Build with the MQL5 Standard Library (`CAppDialog` or chart objects). Size ~560×170 px, semi-transparent dark background, gold header bar.

- **Header bar:** `◆ GoldSweep Scalper` (left) · mode badge `PAPER` / `LIVE` (red when live) · button **[Adaptive Trail: ON/OFF]** · button **[Pause]** (right).
- **Status row:** e.g. `READY · Strategy=sweep_reclaim · Side=BULLISH · Signal=NONE · calc 0.4 ms · ping 8 ms`.
- **Column 1 — Market:** trend (step-trail), ATR(M1) in $, volatility regime (low/normal/high), session/kill zone, spread now vs median, next high-impact news countdown, news bias.
- **Column 2 — Trade:** active signal ID, side, entry, SL, TP1/TP2/TP3 with ✓ when hit, current R, children open (e.g. 2/3), trail mode, time in trade.
- **Column 3 — Performance (today / 30 days toggle):** scalps, win rate vs **breakeven win rate**, profit factor, net P/L $ and R (after costs), average slippage, max drawdown today, daily stop distance remaining.
- Buttons write commands to `scalp_commands.json` (read by the EA) and are also mirrored as Telegram commands.

## 6. In-terminal assistant (`GoldSweepAssistant.mq5` + Python service)

- **MQL5 panel** (floating, draggable, minimisable): left column buttons **Search, New Chat, Clear, History**; quick-command chips **Quick Scalp, Market Status, Twin Bars, Levels, Today's Stats, Explain Last Trade**; a multi-line prompt box (Enter = newline, Shift+Enter = send); a **Get Chart Data ▾** button (dropdown: M1 / M5 / M15 / H1, last 50/100/200 bars); send button; replies shown as chat bubbles with timestamps and copy/refresh icons.
- **Transport:** the panel calls `http://127.0.0.1:8765/assistant` with MQL5 `WebRequest` (document in the README that the user must add this URL in MT5 → Tools → Options → Expert Advisors → "Allow WebRequest for listed URL"). Fallback if WebRequest is blocked: file-based request/response JSON in `Common\Files`.
- **Python service** (`goldsweep/assistant/server.py`, FastAPI + uvicorn, localhost only, token in `.env`):
  - Quick commands are answered by **local deterministic code**, no LLM, and report timing, e.g.
    `Quick Scalp -> SELL (score 78)` / `Strategy=sweep_reclaim; status=READY; market side=BEARISH; entry=4058.34 SL=4067.64 TP1=4053.69 TP2=4049.05 TP3=4044.40` / `(local calculation in 3 ms)`.
    When there is nothing to do: `Quick Scalp -> NONE` plus the reason (e.g. "spread 2.1× median", "news blackout: US CPI in 12 min", "no sweep in last 6 bars").
  - "Quick Scalp (Manual)" in PAPER/LIVE mode only **proposes** a trade; the user must press a confirm button in the panel to send it, and all risk checks still apply.
  - "Get Chart Data" returns a compact summary (trend, last swing points, liquidity levels, ATR, last 3 signals) and stores the raw bars for the LLM.
  - Free-text questions go to `LLM_MODEL` (Claude Haiku 5.5 by default; Sonnet 5.5 optional) with the chart summary + context as input. The LLM can explain and suggest but **cannot place or modify orders**.
  - History is stored in SQLite and searchable (the Search button).

## 7. Mobile + Telegram

- Positions show up automatically in the MT5 mobile app on the same login (the EA runs on the VPS, not the phone). Comments `GS-SC|<id>|<n>` make them easy to recognise.
- Telegram messages: signal (with a PNG snapshot of the chart box rendered by Python/Plotly), each child open, TP1/TP2/TP3 hits, every SL move by the Adaptive Trail, close with net $ and R, daily summary. Commands: `/status`, `/pause`, `/resume`, `/stop` (kill switch), `/trail on|off`, `/stats`.

## 8. Config additions (`config.yaml`)

```yaml
scalper_v2:
  enabled: false
  symbols: [XAUUSD]
  timeframe: M1              # M1 | M5
  strategy: sweep_reclaim    # sweep_reclaim | trail_rejection | momentum_burst | asia_reversion
  step_trail: {atr_period: 10, multiplier: 2.0}
  micro_lookback: 15
  sweep_min_atr: 0.05
  sweep_window: 6
  twin_bars_tolerance_atr: 0.05
  sl_buffer_atr: 0.10
  sl_min_atr: 0.5
  sl_max_atr: 3.0
  tp_step_r: 0.5            # 0.5 = video-2 style ladder; 2.0 = image-02 "wide" ladder
  tp_step_r_max: 2.0
  children: {count: 3, split: [0.4, 0.3, 0.3]}
  adaptive_trail: true
  box_bars: 40
  assistant: {enabled: true, port: 8765, llm_for_free_text: true}
  visuals:
    show_signal_tags: true
    show_step_trail: true
    show_sweep_x: true
    show_twin_bars: true
    show_trend_lines: true
    show_dashboard: true
```
All risk numbers stay in the existing `scalping:` block (risk per scalp, daily limits, cost gate, cooldown, time stop).

## 9. Build phases (stop after each for my approval; show tests + screenshots from the Strategy Tester visual mode)

- **S0 — Review & plan:** read the existing code; list what is reused vs new; confirm file list; note conflicts in `DECISIONS.md`.
- **S1 — Logic in Python:** step-trail, micro-liquidity, sweep "X", twin bars, signal rules, TP ladder, child split, adaptive-trail state machine. Unit tests with small hand-made bar fixtures, including the **video-2 example** as a regression test (entry 4058.34, SL 4067.64 → TP1 4053.69 / TP2 4049.05 / TP3 4044.40, and a stop sequence that only tightens).
- **S2 — MQL5 engine:** `GoldScalper.mq5` v2 with the same logic + Python/MQL5 parity test on exported bars; execution, virtual exits, adaptive trail, time stop, all safety limits; `scalp_context.json` + `scalp_commands.json` bridges with stale-file fail-safe.
- **S3 — Visuals:** `GoldSweepPanel.mq5` v2 (tags, step line, X markers, twin bars, trend lines, trade box with labels and TP-hit markers, dashboard with buttons) in the GoldSweep identity. Provide screenshots from Strategy Tester visual mode for a BUY and a SELL trade, comparable in layout to reference images 01 and 02.
- **S4 — Assistant:** MQL5 panel + FastAPI service, quick commands with timing, Get Chart Data, LLM free text, history/search, WebRequest + file fallback.
- **S5 — Telegram + mobile:** messages, chart snapshots, commands.
- **S6 — Testing & tuning:** Strategy Tester "Every tick based on real ticks" with real commission and measured latency; `.set` presets `tight` (tp_step_r 0.5) and `wide` (2.0); Python tick replay; walk-forward; report per strategy mode, session and spread bucket, gross vs net expectancy, breakeven win rate vs actual. Then a ≥ 4-week demo forward test with a backtest-vs-live comparison.

## 10. Acceptance criteria

- All existing GoldSweep tests still pass; new modules ≥ 80% coverage.
- Python and MQL5 produce the same signals on the same bars (parity test).
- Every child position has a hard SL and TP; total risk of all children ≤ `risk_per_scalp_pct` (test).
- The Adaptive Trail never moves a stop backwards (test).
- No same-direction position can be opened outside the 3 children of the active signal (test).
- EA stops opening trades when `scalp_context.json` is stale or `kill=true` (test).
- Quick commands respond in < 50 ms locally; the LLM is never on the order path.
- No third-party branding, mascot, logo or text appears anywhere in the code or UI.
- README: installing the EAs/indicator, WebRequest whitelist, VPS latency check, Telegram setup, how to read the dashboard, and a plain-language risk section.

## 11. Reference files (in `/reference`)

| File | Use it for |
|---|---|
| `01_overview_M1_buy_box_dashboard.jpg` | Overall chart layout: signal tags, step-trail line, BUY box with TP ladder, dashboard placement |
| `02_sell_box_TP_ladder_step_trail.jpg` | SELL box, sweep "X" marker, TP-hit marker, equal-step TP ladder |
| `03_assistant_chat_panel_M5.jpg`, `03b_assistant_chat_panel_zoom.jpg` | Assistant panel layout and quick-command reply format |
| `04_video2_trade_sequence_01…06` | Full SELL trade lifecycle over time (entry → drawdown → TP1 → trail → TP2/TP3) |
| `05_video2_positions_table_SL_trailing.jpg` | 3 child positions, shared TP, SL moving together, EA comments |
| `06_video3_mobile_stacked_positions_a/b.jpg` | Mobile monitoring view; also the **stacking anti-pattern we must prevent** |

Start with Phase S0.
