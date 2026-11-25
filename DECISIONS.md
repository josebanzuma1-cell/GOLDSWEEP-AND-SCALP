# Decisions

Where the spec (`docs/SPEC.md`) was silent or could be read two ways, this file
records what was chosen and why. Ground rule 7 applies throughout: when in
doubt, the safer option.

## Phase 1: Foundation

### D1. LIVE needs two switches, and the typed confirmation comes with the first order code

The spec puts `MODE=` in `.env` (rule 3, section 12) and `mode:` in
`config.yaml` (section 11) without saying which wins. **Chosen:** the effective
mode is LIVE only when both say LIVE. Any disagreement, a missing value or a
blank value means PAPER. An unrecognised value (for example `MODE=LIVEE`) stops
the program rather than being guessed at.

The typed confirmation at startup is built in Phase 6 together with the first
code that can send an order, with a test that no order path can skip it.
Phase 1 contains no order code at all.

### D2. MetaTrader 5 times are converted from broker server time to UTC

MT5 reports bar and tick times in the trade server's clock, encoded as if they
were UTC. Most servers run on "New York close" time (New York + 7 hours: UTC+2
in winter, UTC+3 in summer). Used as-is, every kill zone, the Asian range and
the news blackout would be two or three hours off, which breaks rule 4.

**Chosen:** `goldsweep/core/server_time.py` converts every timestamp inside the
MT5 client, so nothing above the data layer ever sees server time.
`broker.server_timezone` in `config.yaml` controls it:

- `auto` (default): the offset is measured from a live tick. A tick is trusted
  only after its time has been seen to advance, because a stale tick from a
  closed market can look like a plausible offset. If the measured offset equals
  New York + 7 hours, that rule is adopted (it follows US daylight saving all
  year); otherwise a fixed offset is used.
- The last confirmed rule is remembered in SQLite per server and reused while
  the market is closed.
- Until something is confirmed, New York + 7 is assumed and the client is
  marked unconfirmed. An unconfirmed client withholds the newest bar (D3), and
  later phases will refuse to trade on it.
- A pinned value that contradicts the live server clock is not silently
  replaced: the client reports the mismatch and marks itself unconfirmed.

**Limits:** a broker that follows EU daylight-saving dates differs from New
York + 7 only during about four weeks a year, so `auto` cannot tell them apart
outside those weeks; such a broker should be pinned to `Europe/Athens`. Phase 3
will add a history audit (the daily 17:00 New York break must line up all
year) before any backtest is trusted.

### D3. Only closed bars, judged by the clock

Position 0 in MT5 is usually the bar still forming, but it is a finished bar
when no tick has arrived since the boundary (quiet market, weekend). **Chosen:**
every bar except the newest is closed by definition; the newest counts as
closed only once the clock has passed its end plus a one-second grace period
for late ticks. With an unconfirmed server clock the newest bar is withheld
until 14 hours after its apparent end, the largest plausible error of a guessed
offset.

### D4. `config.yaml` is validated strictly

Unknown keys, duplicated keys, unknown time zones, an auto-trade threshold
below the alert threshold and risk per trade above 2% all stop the program at
startup with one line per problem. A typo that silently fell back to a default
would be worse than a refusal to start. Secrets are rejected in `config.yaml`
for the same reason: they belong in `.env`.

Clock times must be quoted. YAML reads an unquoted `15:00` as the number 900;
that is rejected rather than reinterpreted.

### D5. Additions and one correction to the section 11 starter config

- `sessions.overlap`, `sessions.friday_cutoff` and `sessions.weekend` were
  added because section 4 defines those windows and says all windows live in
  `config.yaml`.
- `broker.server_timezone` was added for D2.
- The starter's `newyork:{tz: ...}` line has no space after the colon, which is
  not valid YAML. It is written as `newyork: {tz: ...}`.

All other values are exactly the section 11 defaults, and a test checks that an
empty config file behaves identically to the shipped one.

### D6. Weekend and "today"

- The weekend runs from Friday 17:00 to Sunday 18:00 New York time
  (`sessions.weekend`). No new trades are allowed in it whatever the session
  windows say.
- "Today" in `--check` is the calendar date in the display time zone
  (Africa/Kampala), not the UTC date, so the table matches the user's own day.
  Each session is then computed for that date in its own time zone.

### D7. The session gate looks at the clock only

`SessionCalculator.status()` answers one question: do the session rules allow a
new trade at this instant (kill zones, overlap, rollover block, Friday cutoff,
weekend)? The news blackout, broker holidays and trade mode, spread and risk
limits are separate checks added in their own phases and combined by the risk
manager. When a kill zone and the overlap are both active, the kill zone names
the session (order: London, New York, Asia, overlap).

### D8. Storage is plain `sqlite3`, and tables arrive with their phase

The spec allows `sqlmodel` or `sqlite3`. **Chosen:** the standard-library
`sqlite3` with numbered migrations tracked in `PRAGMA user_version`: no extra
dependency, and nothing between the code and the SQL. Phase 1 creates the
tables Phase 1 uses (`candles`, `spread_samples`, `app_log`, `kv_state`). The
`news`, calendar `events`, `signals` and `trades` tables from section 3 are
added by later migrations, so their columns are designed alongside the code
that fills them rather than guessed now.

### D9. The mock broker is never a silent fallback

Synthetic prices appear only when asked for (`--check --mock`, or a test). If
MetaTrader 5 is missing, `--check` says so and exits with code 1. Mistaking
generated prices for the market would be worse than an error.

For the same reason the mock check stores its candles and spread sample in its
own file, `data/goldsweep-mock.sqlite3`. The real database only ever receives
data that came from the broker.

### D10. Symbol resolution

`symbol_candidates` are tried in order; the first that exists and is not
disabled wins. If none exists, the account's symbols are searched for
`XAUUSD...` and `GOLD...` names quoted in USD, plainest name first, and a
warning asks for the name to be added to the config. Contract size, digits,
point, tick size and tick value are always read from the broker.

### D11. Connecting to MetaTrader 5

- Blank `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` means "use the account the
  terminal is already logged in to", so the password need not be stored at all.
- If `MT5_LOGIN` is set and the terminal ends up on a different account, the
  connection is refused.
- An `MT5_PATH` that does not exist is an error. Falling back to whichever
  terminal the package finds could attach to the wrong account.
- An account type or symbol trade mode this code does not recognise is treated
  as REAL and as DISABLED respectively: the cautious readings.

### D12. Logging

One JSON object per line in `logs/goldsweep.jsonl` (rotated daily at midnight
UTC, 30 days kept), with records at INFO and above copied to the `app_log`
table. Values under secret-looking keys (`password`, `token`, `api_key`, ...)
are masked before anything is written. Console logging goes to stderr so
command output stays clean.

### D13. Smaller choices

- `--check` prints plain ASCII so it cannot crash on a Windows console code page.
- `tzdata` is a dependency: Windows has no time-zone database of its own.
- Dependencies live in `requirements*.txt`; the project is run from its folder
  and is not packaged.
- The mock aligns D1 bars to UTC days. The engine will not rely on broker D1
  bars: the previous-day high and low are computed from intraday bars with an
  explicit day boundary, decided in Phase 2.

### D14. The interactive check answers within about a minute

- `--check` tries to reach MetaTrader 5 twice, 30 seconds each. The unattended bot
  keeps the client's patient defaults (five attempts, 60 seconds each, exponential
  backoff), because for it waiting is better than giving up.
- MT5 error -10005 ("IPC timeout") means the terminal was found but did not answer.
  This was seen on a freshly installed terminal with no trading account logged in,
  so the message says that and what to do.
- The check **fails** (exit code 1) when a pinned `broker.server_timezone`
  contradicts the live server clock, because every session window would be shifted.
  When the server time merely cannot be confirmed yet (market closed), the check
  passes and says so.
- The report goes to standard output. Log records go to the log file and SQLite
  only, so the report is not interleaved with them.

### D15. Any warning fails the test suite

`pyproject.toml` sets `filterwarnings = ["error"]`. A deprecation in pandas or
another dependency then shows up as a failing test when it first appears, not as a
surprise after an upgrade. The first run already caught one
(`DatetimeIndex.weekday`).

## Phase 2: Analysis engine

The owner asked for Phase 2 to reflect three study guides (market-structure shift,
fair value gaps, liquidity) and "CDT". What they added beyond the spec is marked
*(guide)* below.

### D16. "CDT" is built as CRT, Candle Range Theory

None of the guides mentions "CDT", and no common trading term matches it. CRT
(Candle Range Theory) is the closest: R sits next to D on the keyboard, and CRT is
taught alongside liquidity sweeps. **Chosen:** CRT, in its own module
(`goldsweep/analysis/crt.py`) behind `strategy.crt.enabled`, so it can be
switched off or replaced without touching the rest. **Confirmed by the owner** when
approving Phase 2.

How it is used: the last closed four-hour candle's high and low become liquidity
pools (a raid of either is handled like any other sweep), and a confirmed CRT
(candle 2 took one side of candle 1 and closed back inside) adds score to
signals in its direction for the next two candles.

### D17. The trading day starts at 17:00 New York time

Previous-day and previous-week highs and lows, and the four-hour CRT candles, use
the trading day that gold actually trades: 17:00 to 17:00 New York time,
computed with `zoneinfo` (`strategy.liquidity.day_boundary`). Broker D1 bars are
not used, because brokers cut their days at different hours (D13).

### D18. ATR is the simple average of true range

That is what MetaTrader 5's ATR indicator draws, so the numbers match what the
owner sees on the chart. Wilder's smoothing is available (`strategy.atr_method`).

### D19. Market structure, gaps and liquidity: what the guides added

- **MSS level.** Spec 5.4 says "the most recent swing high formed after the sweep
  began"; the MSS guide says "the last lower high". **Chosen:** the most recent
  *confirmed* swing in the opposite direction, no more than
  `mss_structure_lookback` (30) bars before the sweep. That is the guide's lower
  high when no new swing has formed since, and the spec's swing when one has.
- **Displacement.** A body of at least 1.2 ATR, or a body of at least 70% of the
  range on a candle whose range is at least 1 ATR. The extra range condition is
  the safer reading: without it a tiny full-bodied candle would count.
- **Fair value gaps** *(guide)*. Only a gap whose middle candle is a displacement
  candle and whose size is at least 5% of ATR is used. Entry at the 50% level (the
  guide's "mean threshold") by default.
- **Inversion gaps** *(guide)*. A gap of the opposite direction that price closed
  through after the sweep. The entry priority is gap, then inversion gap, then
  order block (`strategy.entry_zone_priority`).
- **More pools** *(guide)*: unswept swing points (major swings, ten bars clear on
  each side, are *external*; minor ones are *internal*), trendline liquidity
  (three or more touches), London and New York session ranges, CRT range highs
  and lows, and round numbers every $10 with $50 levels counting as major.
  Levels that coincide are merged into one stronger pool.
- **"Current session high/low"** (spec 5.2) is read as the range of each session
  that has already ended in the trading day. The running high of the session in
  progress is not a resting level yet.
- **Premium and discount** *(guide)*: above or below the previous day's midpoint.
  It feeds the bias (spec 5.1) and adds score when the entry is in discount for a
  long or premium for a short.
- **Not built:** breaker blocks, SMT divergence (needs a second instrument) and
  volume imbalances. None is in the spec; they can come later if wanted.

### D20. Which sweeps start a setup

Every pool has a strength from 0 to 1 (previous week 1.0, previous day and Asian
range 0.9, session ranges and equal highs/lows 0.8, CRT and trendlines 0.7,
major swings 0.6, major round numbers 0.5, minor swings 0.4, minor round numbers
0.3; +0.1 for each extra level merged in). Only a sweep of a pool of at least
`min_sweep_pool_strength` (0.5) can become a signal. A pool that price has traded
through by `min_sweep` is treated as taken whatever happens next, and is not re-armed
while the level would still exist (the rest of the day for day and session
levels, up to `max_pool_age_hours` for swing levels).

### D21. Stops, targets and the R:R rule

- Stop-loss: beyond the whole raid's extreme (including any second dip after the
  close back inside) by `0.10 x ATR(M5)` plus the current spread (spec 5.5).
- Take-profits: the nearest and next **external** pools beyond the entry.
  Internal levels (minor swings, round numbers) are things price may react at,
  not targets.
- If R:R to the nearest external pool is below `min_rr`, the setup is **rejected**.
  It is not rescued by aiming at a farther pool.
- A short is closed on the ask, which sits one spread above the chart, so its
  take-profit is placed one spread above the pool to be filled when the chart
  reaches it.

### D22. What the engine checks and what it leaves to the risk manager

The engine rejects a setup when the bar that completes it **opened** outside a
tradeable window, or **closed** inside a hard block (weekend, rollover, Friday
cutoff), or when it points against a LONG/SHORT bias. A NEUTRAL bias allows both
directions but raises both thresholds by `neutral_extra_score` (5 points).
News blackouts, the spread limit, position and loss limits belong to the risk
manager in a later phase. Every rejection is kept with its reason
(`SignalEngine.rejections`) and logged at DEBUG.

### D23. Scoring

Ten components, weights in `strategy.scoring` (they must add up to 100): sweep
quality 20, displacement 15, entry zone 10, Fibonacci 0.62-0.79 confluence 5,
H1 bias 15, news bias 10, kill zone 10, spread 5, premium/discount 5, CRT 5. The
first eight are spec 5.7; the last two come from the guides and D16. Until the
news layer exists (Phase 4) the news bias is 0, which earns half of its 10 points.
A textbook setup in the tests scores 91.1. No score is a promise of a win: the
thresholds only decide between "ignore", "alert" and "may trade".

### D24. One signal per raid, reported once, on time

- When one move sweeps several pools (say the Asian low and the previous day's
  low just under it), only the best-scoring signal is kept.
- The engine reports a signal only on the bar that completes it. When bars were
  missed, they are replayed in order so the state is right, but a setup that
  completed on a missed bar is **not** reported late: its moment has passed.
- A live run and a replay of the same bars give identical signals.
- Signal ids are `YYYY-MM-DD-SESSION-NN` (for example `2026-11-17-LDN-01`).

### D25. Market entries

With `entry_mode: market` the signal fires on the shift candle itself and the
entry is its close. The entry zone is still looked up, but only for the score.
The default stays `fvg_mid`, as in the spec.

## Phase 3: Backtester

The owner approved Phase 2, confirmed CRT (D16 is settled), and asked for a
buyer/seller "volume meter" based on the charts and future news prospects.

### D26. The signal engine runs once per backtest, and its result is cached

Replaying a bar through the signal engine costs about 3.5 ms (after the speed
work below), so 2024-01 to 2026-10 (207,000 M5 bars) took 15 minutes on this
laptop. The signals of a run are saved in `data/backtest-cache/`, keyed by the
price data, the settings and the source of `goldsweep/analysis/`, so a second
report on the same inputs took 9 seconds, and any change to data, settings or
analysis code produces fresh signals.

The speed work changed no result: swing times are converted in one call, bars
are cut with `searchsorted` instead of copied, session windows are cached,
four-hour candles are built with numpy from the last 64 hours only. The Phase 2
tests and a four-week synthetic replay gave identical signals before and after.

### D27. Walk-forward optimises only which signals are traded

Spec 9.3 asks to optimise on 6 months and test on the next 2. **Chosen:** the
grid holds the auto-trade score threshold (65 / 75 / 85) and a minimum R:R
(2.0 / 2.5 / 3.0), nine combinations (`backtest.walk_forward.grid`). These only
select among the engine's signals, so each trial is a fast re-simulation instead
of a 12-minute engine run, and a small grid leaves less room to fit noise.

- The best combination is the one with the highest expectancy in R, among those
  with at least `min_train_trades` (20) training trades; ties go to more total R.
  A window where no combination has 20 trades trades the configured defaults.
- The grid's minimum R:R may not be below `strategy.min_rr`: the engine never
  produces those setups, so they cannot be tested (a config error says so).
- Warnings are printed with `!!!` and shown in red in the report when: there are
  fewer than 30 out-of-sample trades; the in-sample edge disappears out of
  sample; out-of-sample keeps less than half of it; or the chosen settings
  change in more than half of the windows.

### D28. Fills and exits: bid and ask, and the worse case inside a bar

Bars are bid prices (as MT5 draws them); the ask is bid plus spread. A buy limit
fills when the ask reaches it, a short's stop triggers when the ask reaches it.
When the order of events inside a bar is unknown, the worse case is assumed: a
bar touching both stop and target hits the stop; the bar that fills an entry can
stop it out but not take profit; breakeven and trailing moves apply from the next
bar; a limit fills at its own price even after a favourable gap; a gap through
the stop exits at the open. With one-minute history stored (`--with-m1`), all of
this is settled minute by minute instead.

The entry order exists from the bar after the signal, for `entry_expiry_bars`
(6) bars, and is cancelled if price reaches TP1 first (spec 5.5).

### D29. Costs

Defaults (`backtest.costs`), conservative for a gold ECN account; compare with
the broker's real terms:

- spread: the spread stored with each bar (`spread_source: bar`), plus
  `extra_spread_points` (0) as a stress margin; or a fixed spread;
- slippage: 10 points against the trade on market entries and stop exits;
  limit entries and take-profits fill at their price;
- commission: 7.00 per lot round trip, in account currency;
- swap is not charged (the strategy rarely holds overnight), stated as a limitation.

### D30. Trade management in the backtest follows spec section 6

Stop to breakeven at +1R (`breakeven_at_r`), 50% closed at TP1
(`partial_close_at_tp1_pct`), the rest aims for TP2 with its stop trailed behind
confirmed M5 swing points after TP1 (`backtest.trail_after_tp1`). Section 7b's
three-way split (50/30/20 with a TP3) belongs to the live executor of Phase 6;
the backtester will be extended with it then, so the two always manage trades
the same way.

### D31. The risk manager is built now

A backtest that ignores the risk rules overstates results, so
`goldsweep/risk/risk_manager.py` (sizing and limits, no kill switch yet) is built
in this phase and will be the same code paper and live trading use.

- Sizing rounds **down** to the volume step and never up; a stop too wide for the
  minimum lot means no trade. The loss per lot comes from the broker's tick value.
- A pending entry order holds a position slot (`max_open_positions`), so two
  signals cannot both fill.
- `max_trades_per_session` (2) and the spread filter (`max_spread_multiple` 2.0 x
  the `spread_median_days` 20-day median) are in spec section 6 but were missing
  from the section 11 starter file; they are added to `config.yaml`.
- Daily and weekly loss limits compare realised results with the equity at the
  start of the trading day (17:00 New York) and trading week.
- A trade counts as a loss when its net result is below zero, so a breakeven exit
  that lost the commission counts toward the three-losses pause. Safer.

### D32. No news filter in backtests until Phase 4

There is no historical economic calendar yet, so the news blackout cannot be
applied to the past. Every report says so. The risk manager already accepts a
list of events, so Phase 4 only has to supply them.

### D33. The buyer/seller pressure meter is an estimate, shown but not scored

Spot gold has no exchange volume: MT5 "tick volume" counts price changes, not
contracts. The meter (`goldsweep/analysis/pressure.py`) therefore *estimates*
pressure from four readings: where bars close in their range weighted by tick
volume (40%), up-bodies against down-bodies (25%), H1 structure (15%) and the
news bias (20%, from Phase 4, including the outlook for upcoming events). It reads
"Buyers 62% / Sellers 38% (buyers)" and is never described as real order flow.

- It is shown by `python main.py --check` now, and with alerts and on the
  dashboard in later phases.
- It does **not** change signal scores. Every backtest signal records the
  reading, and the report shows results when the meter agreed with the trade,
  disagreed, or was balanced. Whether it earns a place in the score is the
  owner's decision once real history has been tested.

### D34. Smaller choices

- `--to` is inclusive (the whole last day is tested). Trades opened before the
  end are followed to their exit, using up to 5 days of prices after it.
- 10 days of history before `--from` warm the engine up (`backtest.warmup_days`).
- Equity and drawdown are measured at trade closes.
- Report files contain no run time or date, so identical inputs give identical
  files; each report names the fingerprints of its data, settings and code.
- `--mock` prices are a random walk: they produce few setups and say nothing
  about real gold. They exist to check the pipeline.

## Phase 4: News and AI

### D35. Calendar sources and how often they are asked

- **FinanceCalendar.com** is the primary source (spec section 2). It needs
  `FINANCECALENDAR_API_KEY` (a free developer key is approved within 48 hours),
  publishes actual figures, and its terms require a visible credit wherever its
  data appears; every report prints one. Its free plan allows **10 requests a
  day**, so it refreshes every 180 minutes with a daily cap of 10. On a paid plan,
  set `refresh_min: 15` and `fast_refresh_min: 1` for the spec's cadence (every
  minute within 10 minutes of a high-impact release).
- **Forex Factory's weekly JSON feed** is the fallback: no key, this week only,
  no actual figures, personal use. It answered HTTP 429 after a few quick
  requests during development, so it refreshes **hourly**, not every 15 minutes.
- When both list the same release, the first source in config wins. A failing
  source is reported and the other one still runs. Requests are counted per UTC
  day and the last fetch time is remembered across restarts.

### D36. Which releases block new entries

Every high-impact event in `blackout_currencies` (USD), plus any USD event whose
title starts with a word from `blackout_keywords` (CPI, Non-Farm, FOMC Statement,
FOMC Meeting Minutes, FOMC Press Conference, Federal Funds Rate, Fed Chair, PCE,
GDP, ISM, Retail Sales, Unemployment / Jobless Claims). A bare "FOMC" keyword
was tried first: on the live feed it made 26 of 46 USD events blackouts,
because every FOMC member speech matched. The spec names the decision, minutes
and press conference, so member speeches are left out.

### D37. Post-news sweep mode

From 15 to 90 minutes after a blackout release (spec 5.6), the signal engine
accepts a setup even outside the kill zones. It never overrides a hard block
(weekend, rollover, Friday cutoff). Such setups are labelled `post_news` (signal
ids `...-NEWS-01`) and earn the full kill-zone points. Moving the stop of an
open trade to breakeven before a release belongs to the executor (Phase 6).

### D38. Headlines

- Finnhub (`forex` and `general`) when `FINNHUB_API_KEY` is set (sent in a
  header, never in the URL), plus RSS from the Federal Reserve, the ECB and
  FXStreet. Kitco's feed URL now returns 404 and no replacement was found;
  Investing.com's feed gives times without a time zone, so it is not a default.
- One row per story: the key is a hash of the normalised title.
- RSS descriptions become plain text capped at 600 characters, to keep each
  classifier prompt short (spec: ideally under 2,000 tokens).
- Headlines older than 12 hours when first seen are stored but not classified.
- HTTPS certificates are checked against the operating system's trust store
  (`truststore`): the ECB's server does not send its full certificate chain,
  which browsers tolerate and Python's own bundle does not.

### D39. The AI classifier

- Model `claude-haiku-5-5` as the spec names, overridable with `LLM_MODEL`.
- The request asks for the exact JSON schema (structured outputs) and the answer
  is validated again with pydantic. Low effort; no `temperature` (Claude Haiku 5.5
  rejects sampling parameters). The fixed system prompt is marked for caching
  (about 600 tokens, above the model's 512-token minimum).
- An invalid answer is retried once, then the keyword rules decide. A refusal,
  a network or rate-limit error (after the SDK's own two retries) also hands
  that headline to the rules. A rejected key, a missing model or a rejected
  request switches the model off for the rest of the run.
- Spending stops at `llm_daily_budget_usd` (0.50) per UTC day; the rules take
  over until midnight UTC.
- Headline text is data: the system prompt tells the model to ignore any
  instructions inside a headline or summary.
- The model only classifies. It never places or changes a trade.

### D40. The keyword rules (fallback)

Direction is read from the title only: summaries carry unrelated context ("trade
war", dollar safe-haven demand) that misled the first version on live headlines.
Rate-policy words (cuts, hikes, hawkish, dovish) count only next to the Fed or
gold, because a hawkish ECB weakens the dollar. Stories about another currency
pair ("Euro: ...", "USD/JPY ...") count only if they mention gold. Rules always
report confidence 0.35.

### D41. News bias, and what "future news prospects" means here

`news_bias` (-1..+1) is the weighted average of relevant headlines and of US data
surprises, each `direction x impact x confidence`, halving every 2 hours, with
the total weight floored at 1. A data surprise is actual against forecast:
hotter inflation, jobs, growth or retail sales count as bearish for gold,
higher unemployment or claims as bullish; only the direction counts. It needs
FinanceCalendar.com's actual figures.

The owner asked for the pressure meter to use "future news prospects". Upcoming
releases are **not** turned into a direction, because their forecasts are
already in the price. They appear as blackout windows and in the scenario
briefs. Once a release is out, its surprise and the headlines about it move
`news_bias`, which feeds the meter's news part, the H1 bias and the signal score.

### D42. Cost log

Every model call is stored in `llm_usage` with its token counts and an estimated
cost: input tokens at the input price, cache writes at 1.25x, cache reads at
0.1x, output at the output price. Prices live in config (`llm_input_usd_per_mtok`
0.10, `llm_output_usd_per_mtok` 0.50 for Claude Haiku 5.5, prompts under 100K
tokens) because they change. Days are UTC days.

### D43. Scenario briefs

Briefs are due 30 minutes before the London and New York kill zones and 60
minutes before each blackout release. The model writes them from a JSON summary
of the facts when it is available. A template writes them when the model is
off, out of budget or unusable, and also when its answer promises an outcome
(words such as "guaranteed" or "risk-free" are rejected: rule 1). Briefs name
only pools whose sweep the engine would trade (strength 0.5 or more). They are
stored now; Telegram delivery comes in Phase 5.

### D44. Not built in this phase

- FRED macro context (optional in the spec).
- A historical calendar for backtests: backtests still run without the news
  filter and say so (D32).

## Phase 5: Alerts and paper trading

### D45. One trade book for backtests and paper trading

The order and position logic moved out of the backtest simulator into
`goldsweep/execution/book.py`. The simulator and the paper executor both use it,
so a paper trade is filled, managed and costed exactly as a backtested one. The
23 simulator tests passed unchanged after the move, and the hand-made Phase 2
market gives the same trade in both (+227.51 at TP1 and TP2).

### D46. The paper executor

- It contains no code that can send an order to a broker.
- Its whole state (entry orders, positions, the risk counters such as today's
  P/L and the losing-streak pause) is saved in SQLite after every change. A
  restart resumes the same orders and positions, and bars missed while the
  program was down are applied in order.
- The first start does not replay history into the book: trading starts from
  the newest bar.
- Starting equity is the broker account's equity at the first start (the demo
  account), then it follows the paper results.
- Closed trades are kept in the `paper_trades` table, signals in `signals`.

### D47. What happens to each signal

- At or above the auto-trade threshold, with `execution.auto_trade` on and
  trading active: a paper order, if the risk rules allow it (otherwise a
  "blocked" alert with the reason, unless `alerts.send_blocked` is off).
- At or above the alert threshold: an alert only.
- Below that: stored, not sent.
- Signal ids continue from the database (`2026-11-17-LDN-02` after `-01`), so they
  stay unique across restarts.
- One trade per sweep (spec 7b): a signal on the same pool, in the same
  direction, within 12 hours of one already traded, blocked or alerted is
  stored as a duplicate and ignored. This matters after a restart, when the
  engine rebuilds its view of the market and could see the same raid again.

### D48. No order without a stop-loss and a take-profit

A `TradePlan` cannot be created without a finite stop-loss on the losing side of
the entry and a take-profit (TP1, and TP2 when given) on the winning side; the
attempt raises an error. Every order the book holds is built from one. This is
the acceptance test of spec section 10.

### D49. Telegram

- Plain text messages (no Markdown, so no escaping problems), cut at Telegram's
  4,096 characters.
- Only the chat in `TELEGRAM_CHAT_ID` is answered; other chats are logged and
  ignored. Commands are read by long polling and the update offset is saved, so
  a restart does not re-run an old command.
- The bot token is part of the API's URL; it is blanked out of every error.
- Without a token and chat id, alerts are printed to the console instead.
- A message that cannot be delivered is logged; it never stops the trading.

### D50. Commands and the kill switch

`/pause` stops new trades while open trades are still managed. `/resume` starts
again. `/stop` is the kill switch (spec section 6): it cancels every entry order,
closes every position at the current bid or ask, and stops trading until
`/resume`. The state survives restarts. `/status` and `/help` report. The
dashboard button for the kill switch comes with the dashboard (Phase 7).

### D51. Scheduling

APScheduler 3 (spec section 2):
- the bar job runs 2 seconds after every M5 close and keeps asking the broker for
  up to 30 seconds if the bar is late;
- the news job runs every minute (each source still keeps its own schedule);
- the daily summary runs at 21:00 New York time and covers the last 24 hours;
- the command poll runs continuously when Telegram is configured.

One lock serialises all of them, because the MetaTrader 5 Python API is not
thread-safe. LIVE mode is refused until Phase 6.

### D52. Kept for Phase 6

Trade management follows spec section 6 (as in backtests). Section 7b's
three-way split with a TP3, and moving open trades to breakeven before a news
release, come with the live executor and will apply to paper trading and
backtests at the same time.

### D53. The Anthropic SDK loads only when it can be used

Importing the SDK reads thousands of small files: on this laptop it took over
ten seconds and made the paper loop take about 40 seconds to start. It is now
imported at the first possible model call (an API key set, the LLM on), so
keyword-rule runs start in a few seconds.

### D54. A free model provider

The owner asked for a free AI service instead of the paid Claude API. The model
layer is now provider-neutral (`ai/llm.py`, `LLM_PROVIDER` in `.env`):
- **groq** (the suggested free option): Groq's free plan answers through an
  OpenAI-style API and enforces a JSON schema strictly with
  `openai/gpt-oss-20b`, so the answers are checked the same way as Claude's.
  Groq does not keep API inputs by default.
- **anthropic** stays available (and stays the default when `LLM_PROVIDER` is
  empty), so nothing changes for a setup that already works.
- **openai_compatible** covers other services (Gemini's OpenAI endpoint,
  OpenRouter, a local Ollama) with `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.
Google's Gemini free tier was not made the default because its terms allow
free-tier prompts to be used to improve Google's products, including human
review.

GoldSweep does not create accounts or keys: the owner creates the key and puts
it in `.env`. Claude model ids left in `LLM_MODEL` are ignored for other
providers (with a warning), so switching only needs `LLM_PROVIDER` and the key.

### D55. Staying inside a free plan

The free plan's limits are counted in tokens (8,000 a minute and 200,000 a day
for this model when this was written). One headline costs about 1,000 tokens, so
GoldSweep caps Groq at 6 calls a minute and 180 a day (settings
`news.llm_max_calls_per_minute` / `_per_day` override the defaults). When the
minute is full, the remaining headlines wait for the next minute instead of
going to the rules; when the day is full, the rules classify until 00:00 UTC.
Headlines the keyword rules find unrelated to gold skip the model altogether
(`news.llm_prefilter`). Any 4xx answer other than 429 (bad key, unknown model)
turns the model off for the run and says why; 429 and network errors fall back
to the rules for that headline only. Free calls are recorded at US$0 so the
usage report still counts them.


### D56. Three targets in every mode (spec 7b.3)

The split now applies to backtests, paper and live trading alike (it lives in
`execution/book.py`, `plan_legs`): `partial_close_at_tp1_pct` (50) at TP1,
`partial_close_at_tp2_pct` (30) at TP2, the rest at TP3. The spec allows TP3 at
"3R or session extreme"; GoldSweep uses **3R** (`risk.tp3_r`), because TP1 and TP2
already are the nearest pools, which include the session highs and lows, and a
fixed R multiple is predictable. When 3R is not beyond TP2 there is no TP3 and its
share goes to TP2 (the safer, nearer target); likewise a missing TP2 gives its
share to TP1. When a part would be below the broker's minimum volume, the
farthest part is folded into the one before it. `tp3_r: 0` restores the
two-target split of Phase 5. Backtest results change slightly as a result.

### D57. Breakeven before a release (spec 5.6)

From the start of a high-impact blackout until the release, a trade at least
`news.breakeven_before_news_r` (1.0) R in profit moves its stop to breakeven. With
`breakeven_at_r: 1.0` most such trades are already there; the rule matters when
`breakeven_at_r` is raised. Backtests apply it at the bar in which a blackout
starts, using the close.

### D58. Live sizing and caps

Live sizing uses the broker's balance and, for market orders, the price the
order will actually get, so the parts together never risk more than
`risk_per_trade_pct` even when price has moved since the signal. Then
`max_total_lots` (1.0) can only cut the size, never raise it, and the margin
check refuses an order that would take the margin level below
`min_margin_level_pct` (1000%), as spec 7b.7 asks. A limit entry whose price has
already been passed becomes a market order at the better price (smaller risk),
unless the price is already beyond the stop or TP1, in which case nothing is sent.

### D59. Talking to MT5

`order_check` runs before every `order_send`; it is also how the filling mode is
chosen (the symbol's FOK and IOC flags, then RETURN; RETURN first for resting
orders), because servers differ. Requotes, price changes, timeouts, "too many
requests" and lost connections are retried three times with backoff (0.5 s, 1 s)
and a fresh price; every other answer is final and reported. Prices are rounded
to the symbol's digits. Limit orders are good-till-cancelled and GoldSweep
cancels them itself at expiry, so no broker expiry mode is needed.
Checked against the real Exness demo server with `order_check` (no order sent):
market orders are accepted with FOK, limit orders with RETURN. That check also
found that the MetaTrader5 package rejects `order_check(request, **{})` ("Unnamed
arguments not allowed"): the client never passes empty keyword arguments.

### D60. The broker is the record

Fills, exits and results come from MT5 (positions, then the deal history), never
from simulated prices. The bot keeps only what it wants (each part's ticket and
the wanted stop) and pushes the stop to the broker until it is accepted. A
position that vanished is waited for up to 60 seconds for its deals to appear in
the history. Exit reasons: the broker's take-profit = that part's target; its
stop = stop, breakeven or trail depending on where the stop was; anything else
(the kill switch, a close by hand) = manual.

### D61. Reconciliation at startup

The state is saved in SQLite (`live.book`) after every change. At startup what
happened while the bot was off is read from MT5 and reported. GoldSweep positions
that are not in the saved state (the database was lost or replaced) keep their
own stop and target and are reported, but not managed: guessing their plan could
move a stop the wrong way. One without a stop or target is closed for safety, and
unknown GoldSweep entry orders are cancelled, so nothing unmanaged can open later.

### D62. The typed confirmation

Spec rule 3 asks for "a typed confirmation". GoldSweep asks for `LIVE <account
number>` after showing the account, balance, risk and lot cap, so the owner
confirms *this* account, not just "yes". No answer (for example when started
without a console) means no.

### D63. Demo only in Phase 6

Spec phase 6 says "demo account only". `--live` refuses accounts MT5 reports as
real or contest unless `execution.allow_real_account: true` is set in config.yaml,
which should only happen after the demo results have been reviewed. It also
refuses to start while MT5's Algo Trading is off, saying how to turn it on.

### D64. Not logging in again

MT5 treats a login to the account it already shows as an account change, and its
option "disable algorithmic trading when the account has been changed" then turns
the Algo Trading button off. The client therefore logs in only when the terminal
is on another account (seen on 2026-11-18: Algo Trading read as off after every
connection).

### D65. Optional scale-in not built

Spec 7b.4 describes an optional add-on entry, off by default. It is not built:
it adds exposure to a winning trade and is not needed for the core strategy. The
default behaviour (no scale-in) is what the spec asks for.

### D66. The chart panel

Python and MQL5 share only `Common\Files\GoldSweep\signals.json` (spec 7b.5): the
indicator reads it, never writes it, and never trades. Times are written in the
broker's server clock (the chart's clock), prices rounded to the symbol's digits,
one signal per line with Windows line ends, so the MQL5 side stays a simple line
reader. The file is replaced atomically and retried if the indicator is reading
it. The indicator compiled with 0 errors and 0 warnings in MetaEditor (build 6320).


### D67. The dashboard reads the database; the bot does the trading

Spec 9.7 asks for a Streamlit dashboard with a live chart, scenarios, the news
feed, open trades, risk status, the equity curve and backtest reports; spec 6
adds a kill-switch button. The dashboard never connects to MetaTrader 5: a second
program on the same terminal could interfere with the bot, and only one place
should send orders. The bot stores what the page needs after every M5 bar
(the last 300 M5 candles, and a snapshot of price, pools, H1 bias, pressure, news
bias, equity, positions, entry orders and risk counters, in `kv_state`), and a
heartbeat every 10 seconds. Fair value gaps are worked out again on the stored
candles with the analysis code, and shown while price has not traded back
through them.

### D68. Commands through the database

The Control tab writes pause / resume / status / stop into a `commands` table
(migration 5). The bot reads it every 2 seconds, carries the command out exactly
as the Telegram command, stores the reply, and also posts it on Telegram. A
command older than 2 minutes is not carried out (marked expired): a kill switch
pressed while the bot was off must not surprise anyone at the next start. With
no recent heartbeat, the buttons are off and the page explains how to close
positions in MT5 directly.

### D69. Only on this PC

The page has a kill-switch button, so Streamlit is bound to `localhost`: other
devices on the network cannot open it. Its usage statistics are off (privacy),
it runs headless (no first-run e-mail prompt that leaves the window waiting) and
the browser is opened by GoldSweep instead. Remote access, if ever wanted, belongs
to Phase 8 (a VPS) with proper authentication.

### D70. Streamlit and Plotly

Streamlit 1.67 and Plotly 6.9 (spec 2 names Streamlit). The page refreshes
itself with a timed fragment, opens a short-lived SQLite connection on each
refresh (nothing stays open across Streamlit's threads), and puts the project
folder first on Python's path, because `streamlit run` starts the page as a
script and `goldsweep` could otherwise resolve to the wrong folder.

### D71. Telegram commands in their own thread (a Phase 6 fix)

The Telegram poll waits up to 10 seconds for a message. Scheduled every second,
it overlapped itself and the scheduler logged a "skipped" warning every second
(about 86,000 a day, into the log file and the database). It now runs in its own
thread, one long poll after another; the scheduler only runs the bar, news,
summary, sync and dashboard jobs.

### D72. Figures on the dashboard

The Trades tab shows plain counts and sums of the stored trades (trades, won and
lost, net, total R, profit factor), with the note that past results do not
predict future ones. The equity curve is worked back from today's equity and the
trades' results, so it needs no separate record of the starting balance. The
pressure meter is labelled as an estimate, as everywhere else.


### D73. The typed confirmation after an unattended start

Spec rule 3 asks for a typed confirmation at startup; spec 9.8 asks for an
automatic restart. Both hold: a start with `--unattended` (the scheduled task)
asks nothing at the console and starts **unarmed**: it reads the market, alerts
and reports, but sends no order of any kind (no entries, no stop moves, no
expiry cancellations) until the owner types `/golive <account number>` on
Telegram, from the one chat the bot answers. The kill switch (`/stop`) still
works before that, because it only reduces exposure on the owner's command.
Open trades keep their stop-loss and take-profit at the broker meanwhile; the
held-back stop moves are sent at the confirmation. This repeats after every
restart, so the owner always decides when the bot trades. An unattended start
without Telegram configured is refused (nobody could confirm it).

### D74. Task Scheduler, not a Windows service

MetaTrader 5 is a desktop program and its Python link needs the same logged-on
session, so GoldSweep cannot run as a service (services run in a session without
a desktop, which rules out NSSM here). The tasks run at logon for the owner's
user, without administrator rights. The bot task repeats every 5 minutes with
"do not start a second copy": a running bot is left alone, a stopped one is
started again. The bot and the watchdog run under `pythonw.exe`, with no console
window: on 2026-11-18 a click inside the bot's console paused it (Windows
"Select" mode), which cannot happen without a console.

### D75. Heartbeat and watchdog

Spec 9.8: a heartbeat every hour, an alert when it stops. The bot sends a quiet
Telegram message every `alerts.heartbeat_minutes` (60; no sound). A separate
check, `--watchdog`, runs every 15 minutes from Task Scheduler and reads the
bot's 10-second heartbeat in SQLite: while the gold market is open (Sunday 18:00
to Friday 17:00 New York time, outside the daily 17:00-18:00 pause) a heartbeat
older than `alerts.watchdog_minutes` (15) sends one alert, and the next fresh
heartbeat an all-clear. It runs on the same machine, so it cannot report a
machine that is off: the missing hourly heartbeat is the sign of that, and a VPS
the remedy.

### D76. Daily upkeep

Log files already rotate daily and keep 30 days. Every day at 00:15 UTC the bot
copies the database with SQLite's online backup (consistent while it is being
written) to `data/backups`, keeps the newest 14, and removes log rows older than
30 days from the database. Candles, signals, trades and the order log are kept:
they are the record.

### D77. Netting accounts refused

GoldSweep splits a trade into separate positions with their own targets
(D56). On a netting account MT5 merges them into one position whose stop and
target the last order would overwrite, so `--live` refuses such an account and
says why. The owner's Exness demo account is a hedging account.

### D78. News work outside the trading lock

The news refresh (feeds, the calendar and AI calls, which can take tens of
seconds) no longer holds the lock that serialises the MT5 calls: it never touches
the broker, and the bar job must not wait for it. The dashboard job writes its
heartbeat without the lock and leaves commands for its next run when the lock is
busy, so it never piles up behind a long job.

### D79. Automatic logon and remote access

After a restart, MT5 needs a logged-on desktop, so a VPS needs automatic logon
(Microsoft's Autologon tool stores the password encrypted). The guide recommends
it for a VPS and asks the owner to weigh it on a personal laptop. Remote Desktop
users should close the window rather than sign out (signing out stops MT5). The
dashboard stays bound to the machine itself (D69): it is reached over Remote
Desktop, not exposed to the internet.

### D80. Trading outside the kill zones is an option, off by default

On 2026-11-20 the owner suggested scanning for setups at all hours rather than
only in the London and New York kill zones. Spec section 4 trades only the kill
zones, so `sessions.trade_all_hours` keeps that as the default (false). When set
to true, new trades are allowed at any hour the market is open; the rollover
block, the Friday cutoff, the weekend, the news blackouts and every risk limit
still apply, and a setup outside the kill zones gets no kill-zone points (10 of
100), so it needs to be stronger elsewhere to reach the auto-trade threshold.
Whether to switch it on for the demo account is decided on evidence: the same
real history (2026-01-01 to 2026-11-19) is backtested both ways, and the
out-of-sample results are compared.

**Result (2026-11-21): it stays off.** Exness demo feed, XAUUSDm M5, 65,163
bars (2025-12-22 to 2026-11-20 with warm-up; no bars on 2026-04-03), the default
settings, both runs on the same stored prices:

| | Kill zones only (default) | All hours |
|---|---|---|
| Setups found | 48 | 137 (89 outside the kill zones) |
| Trades at the auto-trade threshold (75) | 3: -0.34 R per trade, -45.77 USD | the same 3 trades |
| Out-of-sample (walk-forward, 3 windows) | 1 trade, -1.01 R | the same 1 trade |
| Stress check at the alert threshold (60) | 10 trades: +0.34 R per trade, profit factor 1.86 | 19 trades: -0.04 R per trade, profit factor 0.92 |
| of which outside the kill zones | - | 9 trades: -0.48 R per trade, profit factor 0.30 |

At the threshold the bot trades, the switch changes nothing. Only 3 of the 89
extra setups scored 75 or more, and none became a trade: one limit entry expired,
one was cancelled because price reached TP1 first, and one needed 80 because the
H1 bias was neutral. With the bar lowered to 60 to get more trades, the extra
out-of-zone trades lost on average. Every sample here is far too small to prove
anything (the report asks for at least 30 out-of-sample trades), so this is no
evidence for the change and weak evidence against it; the safer default stays.

A wider finding: the strategy took 3 trades in this period at 75. Of the 10
kill-zone setups that scored 75 or more, 6 expired before price reached the
entry and 1 needed 80 because of a neutral bias. Every
walk-forward training window had fewer than 20 trades, so the configured defaults
were traded rather than optimised ones. A longer history (the spec's 2024-01-01
to 2026-10-31, which needs MT5's Max bars in chart set to Unlimited) is needed
before any setting can be judged on results.

Reproduce (reports are git-ignored): `python -m goldsweep.backtest --from
2026-01-01 --to 2026-11-19`, then the same with `--config` pointing to a copy of
`config.yaml` with `trade_all_hours: true` and `--out` set to another folder; add
`--threshold alert` for the stress check.

### D81. The first full real-history backtest: too few trades at 75

The spec's acceptance command, `python -m goldsweep.backtest --from 2024-01-01
--to 2026-10-31`, was run on 2026-11-21 on the Exness demo feed: XAUUSDm M5,
203,442 bars (2023-12-22 to 2026-11-06; no bars on the Good Fridays), default
settings. The replay took 10 minutes.

| | Auto-trade threshold 75 (the setting) | Alert threshold 60 (`--threshold alert`) |
|---|---|---|
| Setups found | 136 (about 45 a year) | the same 136 |
| Trades | 4: none won, -0.51 R per trade, -1.0% | 31: 19% won, +0.40 R per trade, profit factor 1.96, +5.9%, max drawdown 2.6% |
| Trades from 2024-07 on (walk-forward test windows) | 3: -0.34 R per trade | 27: +0.38 R per trade, profit factor 1.88 |
| By session | too few to split | London 16 (+0.93 R), New York 12 (-0.03 R), overlap 3 (-0.68 R) |

Why so few trades at 75: the median setup scores about 67, and only 21 of 136
reach 75 (a neutral H1 bias asks for 80, D80). Of those, 12 expired because price
never came back to the entry (the middle of the gap, valid for 6 bars). At 60,
50 of 136 setups expire the same way. The news part of the score is neutral in
the backtest (5 of its 10 points), so a live score can be up to 5 points higher
or lower than the backtest's for the same setup.

What it does and does not show:

- At 75 the strategy trades about once or twice a year. Neither a backtest nor
  the demo account can judge it at that rate: 30 trades would take well over a
  decade.
- At 60 the results are positive, carried by London and by a few large winners
  (fewer than one trade in five won). This is not proof. 27 trades are still
  below the 30 the report asks for. The walk-forward never optimised (every
  training window had fewer than 20 trades, so it traded the defaults), so its
  "out-of-sample" figure is the same fixed setting on later months, not an
  independent test. And 60 is being looked at after seeing it do better than 75,
  which is itself a form of fitting to the past.
- Not modelled: the news blackout (no stored historical calendar), the news part
  of the score, one-minute fills, and spread spikes.

**Decision (owner, 2026-11-21):** `auto_trade_threshold` is set to 60 in
`config.yaml` for the demo account, equal to the alert threshold, so every
setup that is worth an alert is also traded when the risk rules allow (65 while
the H1 bias is neutral). This is a way to collect evidence at a usable rate,
about ten trades a year on this history, not a claim that it will be profitable.
Risk per trade and every limit are unchanged, and `--live` still refuses a
real-money account. The code default stays 75, as in the spec. Before any
real-money use, the demo results are reviewed and this threshold is decided
again. The expiry of setups before their entry is the largest leak and the next
thing worth testing (entry price and `entry_expiry_bars`), each test being a
10-minute replay.

## Phase 9: entries that fill, sniper entries, more setup models

The owner asked (2026-11-21) to fix the low trade count and to add "sniper"
entries and further strategies, researched and applied. The phase runs in three
stages, each ending with tests, a 2024-2026 backtest and a stop for approval:
(1) entries that fill, (2) M1 refinement, (3) more setup models. New models will
alert first and trade only once their own backtest supports it and the owner
approves (the safer option; the owner skipped the question).

### D82. Entries wait two hours, and never into a blocked time

**Why setups expired.** Of the 110 setups scoring 60 or more in 2024-2026, 64
expired. In 61 of the 110 price came back to the gap's near edge within 30
minutes but not to its middle, where the order sat: the entry was well placed but
withdrawn too early.

**A waiting entry is withdrawn before any blocked time.** Before Phase 9 a
waiting order was removed only by its expiry, by TP1 being reached first or by the
kill switch, so it could fill inside a news blackout. Now `entry_deadline` (in
`execution/book.py`, shared by the backtester, paper and live trading) brings a
plan's expiry forward, when it is placed, to the first of: a known high-impact
blackout, the rollover block, the Friday cutoff or the weekend (outcome
`withdrawn`, and the alert says "valid until 16:45 UTC (before the rollover
block)"). A release the calendar adds later is caught by the minute news job,
which withdraws waiting entries a minute before its blackout. While an
unattended start is unarmed, no cancellation is sent either (only the kill switch
acts then), as for expiries. Leaving a kill zone does not withdraw an entry: the
setup formed inside it.

**New entry prices.** `fvg_near` / `fvg_far` (the edge a pullback reaches first /
last, whichever the direction) and `ote` (the middle of the fib_low..fib_high
retracement from the move's end back toward the sweep's wick, 70.5% with the
defaults, held inside the zone). Zone choice for `ote` uses the gap's middle.

**Testing variants cheaply.** How long an entry waits changes no signal, so
`entry_expiry_bars` left the signal cache key; the simulator applies the
configured lifetime, and the walk-forward grid now includes it (6 / 12 / 24 bars).
The entry price does change which setups pass the R:R filter and earn fib points,
so each entry mode still needs its own replay (planned as cheap, found not to be).

**Results, 2024-01-01 to 2026-10-31,** threshold 60, everything else default:

| Entry | Waits | Trades | Per trade | Profit factor | Return | Max DD | From 2024-07 on |
|---|---|---|---|---|---|---|---|
| gap middle | 6 bars (before) | 31 | +0.40 R | 1.96 | +5.9% | 2.6% | 27 trades, +0.38 R |
| gap middle | 12 bars | 44 | +0.27 R | 1.65 | +5.9% | 3.4% | 38, +0.28 R |
| **gap middle** | **24 bars** | **54** | **+0.52 R** | **2.23** | **+14.4%** | **2.5%** | **44, +0.38 R** |
| near edge | 6 / 12 / 24 | 36 / 45 / 51 | +0.34 / +0.27 / +0.42 R | 1.71 / 1.49 / 1.86 | +5.7 / +4.9 / +9.9% | 3.3 / 4.3 / 2.5% | 32 / 39 / 43 trades, +0.28 / +0.26 / +0.36 R |
| OTE | 6 / 12 / 24 | 29 / 42 / 57 | +0.26 / -0.02 / +0.36 R | 1.47 / 0.92 / 1.67 | +3.5 / -0.9 / +9.9% | 3.4 / 5.9 / 4.4% | 24 / 35 / 47 trades, +0.26 / -0.03 / +0.27 R |

**Decision:** keep the gap's middle and let it wait 24 bars (2 hours):
`entry_expiry_bars: 24`. It nearly doubles the trades and did at least as well as
before on every measure shown. Read with care:

- 12 bars did worse than both 6 and 24, which says how noisy samples of 30-60
  trades are; the ranking is partly chance.
- The walk-forward still never optimised (every window had fewer than 20 trades),
  so "from 2024-07 on" is the same fixed setting on later months, and 24 was
  chosen after looking at the whole period: a mild fit to the past.
- The result leans on a few big winners: the 3 best trades carry about 19.5 of the
  28 R. By year: 2024 +19.2 R (22 trades), 2025 +8.5 R (15), 2026 +0.3 R (17).
  About one trade in five wins; the average win is +4.2 R, the average loss -0.54 R
  (many losers leave at breakeven), and the longest losing run was 12 trades.
- No historical calendar is stored, so news blackouts were not part of these
  backtests; live and paper trading apply them (and now withdraw entries before
  them).
- A waiting entry holds the one position slot for up to 2 hours, so it can block
  a later setup; the backtest includes that.

The spec's 6 bars stays the code default; `tests/test_settings.py` lists this as a
recorded change.

### D83. M1 "sniper" entries: built, tested, and left off

**What it does** (`analysis/refine.py`, `strategy.refinement`). With `mode: m1` an
M5 setup that would trade is *armed* instead: no order is sent. Each minute the
closed M1 bars are checked. When price has come back to the gap's near edge and
then turns (a close through the last M1 swing high before the pullback's low,
`choch`, or through the open of the last run of down-closing candles into it,
`cisd`; shorts mirrored), the order is placed at that close (`market`) or as a
limit in the middle of the M1 gap the turn left (`fvg_mid`). The stop goes beyond
the pullback's extreme (0.2 M1 ATR plus the spread), never closer than 4 spreads or
1 M1 ATR and never beyond the M5 stop; the M5 targets stay. Nothing is traded when
the entry window ends first, when price reaches TP1 before the gap, or when it
trades through the M5 stop (`no_trigger`). The refiner is a pure function of the
setup and the closed M1 bars, so a live run, a restart (armed setups are saved) and
a backtest agree. Live: a one-minute job; the alert says "waiting for an M1 turn
inside the gap until ..."; the refined order gets its own message (🎯) and goes
through the same risk checks, deadline rule and executor as any other order; the
kill switch drops armed setups. The backtester needs the M1 history and downloads
it when refinement is on (1,014,942 bars for 2024-2026).

**Results, 2024-01-01 to 2026-10-31,** M1 fills for every variant, threshold 60,
entries wait 24 bars (D82):

| Entry | Trades | Won | Per trade | Profit factor | Return | Max DD | From 2024-07 on | By year (R) |
|---|---|---|---|---|---|---|---|---|
| **M5 gap middle (no refinement)** | **54** | **20%** | **+0.47 R** | **2.10** | **+12.3%** | **2.4%** | **44, +0.40 R** | **+15.9 / +8.5 / +0.7** |
| M1 choch, market | 40 | 10% | -0.17 R | 0.70 | -3.2% | 5.2% | 35, -0.10 R | -3.2 / +1.8 / -5.3 |
| M1 choch, M1 gap | 29 | 10% | +0.02 R | 1.03 | +0.2% | 3.2% | 25, +0.07 R | -6.5 / +6.1 / +0.9 |
| M1 cisd, market | 51 | 10% | +0.29 R | 1.49 | +6.2% | 4.1% | 45, +0.35 R | -3.9 / +23.9 / -5.4 |
| M1 cisd, M1 gap | 47 | 11% | +0.63 R | 2.24 | +13.6% | 3.3% | 43, +0.72 R | -6.8 / +37.5 / -1.2 |

**Decision: `refinement.mode` stays `none`.** The tighter stop is hit about twice
as often (one trade in ten wins instead of one in five), so the result rests on a
few very large winners. The best-looking variant (cisd, M1 gap) had 5 winning
trades out of 47; its top 3 trades carry the whole result (without them it is
-11.9 R, against +6.0 R for the plain entry without its top 3), and it lost money
in two of the three years. The plain M5 entry is steadier: positive or flat in
every year. On this history the sniper entry does not earn its place; it stays in
the code, tested, to be switched on only if later evidence (a longer history, or
demo trading) says otherwise.

### D84. Exit management: the +1 R breakeven move cuts winners short

**Why it was tested.** The owner asked how to make the normal entry far more
accurate (a 95% win rate). Replaying the 54 trades of 2024-2026 on M1 bars showed
that no exit can do that: with the original stop, a target of +0.5 R would have
been reached first 70% of the time and earned almost nothing (+0.06 R per trade),
and 16 trades never went even +0.5 R in favour. But 44% of the trades reached +3 R
before their stop, while only 20% ended as wins: the management was closing many
of them at the entry before they ran.

**New options** (shared by the backtester, paper and live trading):

- `risk.breakeven_at_r: 0` turns off the move to breakeven by distance. Reaching
  TP1 still moves the stop to the entry (spec 7b), as the owner asked to keep.
- `risk.target_mode: fixed_r` with `risk.fixed_targets_r` puts the targets at fixed
  R multiples from the entry instead of at the liquidity pools: one value closes
  everything there, two or three split the position as
  `partial_close_at_tp1_pct` / `_tp2_pct` say (`TradePlan.with_exit_targets`). The
  setup itself still needs a pool at least `min_rr` away. The alert shows the
  targets actually used.

**Results, 2024-01-01 to 2026-10-31,** M1 fills, threshold 60, entries wait 24 bars,
everything else default. A screen of 24 variants (breakeven at +1 / +2 / +3 R or
only at TP1, by six target layouts); the best ones and today's rule then went
through the walk-forward ("from 2024-07 on", the same fixed rule on later months):

| Exit rule | Trades | Won | Per trade | Return | Max DD | Losing run | By year (R) | Without the best 3 | From 2024-07 on |
|---|---|---|---|---|---|---|---|---|---|
| Today: breakeven at +1 R, pools 50/30/20 | 54 | 20% | +0.47 R | +12.3% | 2.4% | 12 | +15.9 / +8.5 / +0.7 | +6.0 R | 44, 20% won, +0.40 R |
| **Breakeven only at TP1, pools 50/30/20** | **55** | **36%** | **+0.93 R** | **+26.7%** | **2.9%** | **6** | **+18.6 / +19.8 / +12.7** | **+29.8 R** | **45, 36% won, +0.89 R** |
| Breakeven only at TP1, all at 3 R | 51 | 45% | +0.79 R | +20.1% | 2.3% | 4 | +15.6 / +16.8 / +7.9 | +31.2 R | 41, 44% won, +0.74 R |
| Breakeven only at TP1, all at 5 R | 53 | 34% | +1.02 R | +28.1% | 2.9% | 6 | +21.5 / +25.7 / +6.8 | +39.1 R | 44, 34% won, +1.03 R |
| Breakeven at +3 R, pools 50/30/20 | 54 | 33% | +0.83 R | +23.6% | 3.0% | 7 | +19.7 / +11.4 / +14.0 | +24.7 R | 44, 32% won, +0.74 R |
| Fixed 2 R, one target (any breakeven) | 51 | 43% | +0.28 R | +6.6% | 4.1% | 9 | +3.6 / +11.8 / -1.1 | +8.3 R | |

**What it shows.** The pattern is the same for every target layout: the later the
distance breakeven, the better (off > +3 R > +2 R > +1 R). Gold often comes back to
the entry after +1 R before it runs, so the +1 R move turned trades that later
reached their targets into zero results. Without it the win rate rose from about
one in five to about one in three, the average result roughly doubled, the losing
runs halved, every year was clearly positive, and the result no longer depends on
three trades. A near fixed target (2 R) wins often but earns little: accuracy on
its own is not the goal.

**Read with care.** 24 variants were tried on the same history, so the best of
them is flattered; in-sample the leading rule made +1.11 R per trade and +0.89 R on
the later months. The walk-forward still never optimised (every window had fewer
than 20 trades), samples are about 50 trades, and no historical calendar is stored.
A trade can still lose its full 1 R until TP1 is reached; the drawdown rose from
2.4% to 2.9%.

**Decision (owner, 2026-11-21): breakeven only at TP1, with the pool targets.**
`config.yaml` sets `breakeven_at_r: 0`; everything else is unchanged. It is the
smallest change, the steadiest by year, and it keeps the spec's liquidity targets
and the rule the owner asked to keep. (Fixed 3 R was the alternative for the
highest win rate, about 45%, and the lowest drawdown, at a lower average.) The
spec's 1.0 stays the code default; `tests/test_settings.py` lists the change. The
acceptance backtest with this setting is in `reports/backtest_2024-01-01_2026-10-31`.

### D85. More setup models and confluence tags: built, measured, alert only

**What was built** (Phase 9 stage 3, from the owner's request to apply further
strategies after research; the sources are community and ICT write-ups with no
tested evidence, so everything was measured on this history first).

- **Models**, each its own `SignalEngine(model=...)` on the same bars
  (`analysis/signals.py::MODELS`), so a new model can never change or crowd out the
  spec's setups; their trades share one set of risk limits:
  - `cisd`: the spec's pipeline confirmed by a change in the state of delivery (a
    close through the open of the last run of candles into the sweep's extreme,
    `structure.find_cisd`) instead of the market-structure shift;
  - `silver_bullet`: the spec's pipeline only inside the New York 03-04, 10-11 and
    14-15 hours (zoneinfo), outside the kill zones too;
  - `continuation`: sweeps of internal (minor) pools, only with an H1 trend.
  Turtle Soup needs no model of its own: sweeps of swing and session pools already
  are failed breakouts.
- **Confluence tags** on every signal (`Signal.tags`): `unicorn` (the gap overlaps a
  breaker block, `zones.find_breaker`), `bpr` (it overlaps an opposite gap from the
  drop: a balanced price range), `judas` (the raid ran beyond the New York midnight
  open), `silver_bullet` (formed in one of those hours), `ote` (the entry is in the
  62-79% pullback), `smt` (gold swept a low or high while silver, over the same bars,
  held its own: `analysis/smt.py`, silver = XAGUSDm on the Exness demo, 203,264 M5
  bars stored). Alerts list them ("Confluence: ...").
- **Deviations from the stage plan, both the safer option.** The tags do not
  score: re-weighting the 100 points to make room would have changed which of the
  validated setups pass 60 (D81, D84). Breaker, Unicorn and balanced price range are
  tags, not new entry zones, for the same reason.
- **Switches**: `config.yaml models:` (enabled / trade per model; `sweep_mss`
  trades, the others alert only) and `confluence.smt`. In paper and live trading
  each enabled model sends its setups; one that does not trade says so in the alert
  ("alert only: the CISD model does not trade yet"). The duplicate check is per
  model, so an alert-only model never stops the trading one from taking a raid.
  Backtests generate signals only for the models that trade, each cached separately,
  and report results by model and by tag.

**Results, 2024-01-01 to 2026-10-31,** M1 fills, every model alone under today's
settings (threshold 60, entries wait 24 bars, breakeven only at TP1):

| Model | Trades | Won | Per trade | Return | Max DD | By year (R) | Without the best 3 | From 2024-07 on |
|---|---|---|---|---|---|---|---|---|
| **sweep + MSS (the spec's)** | **55** | **36%** | **+0.93 R** | **+26.7%** | **2.9%** | **+18.6 / +19.8 / +12.7** | **+29.8 R** | **45, +0.89 R** |
| CISD | 120 | 23% | +0.23 R | +13.5% | 8.2% | +27.6 / +0.9 / -0.8 | -0.4 R | 99, +0.13 R |
| Silver Bullet | 16 | 31% | +0.72 R | +5.3% | 2.5% | +2.6 / -0.7 / +9.7 | -5.5 R | 12, +0.41 R |
| Continuation | 17 | 24% | +0.86 R | +6.8% | 4.0% | +19.8 / -0.3 / -4.9 | -10.4 R | 15, +1.13 R |

The spec's model by confluence tag (55 trades, +0.93 R per trade overall; a trade
can carry several tags):

| Tag | Trades | Won | Per trade | Profit factor |
|---|---|---|---|---|
| unicorn | 37 | 43% | +1.22 R | 3.16 |
| judas | 49 | 41% | +1.17 R | 2.93 |
| ote | 11 | 36% | +1.18 R | 3.10 |
| silver_bullet | 14 | 36% | +0.97 R | 2.51 |
| smt | 30 | 37% | +0.79 R | 2.09 |
| bpr | 23 | 35% | +0.71 R | 2.12 |

Without the Unicorn tag the other 18 trades made +0.34 R each; without the Judas tag
the other 6 lost -1.02 R each.

**Decision: no new model trades; all stay alert only.** CISD doubles the trades but
earned almost everything in 2024, has a drawdown nearly three times the spec's and
nothing left without its best three. Silver Bullet and continuation found too few
trades to judge (16 and 17) and lose without their best three. The tags do not
change the score either. Unicorn and Judas look worth watching (the setups that
carried them did clearly better), but these are six tags compared after the fact on
about 50 trades: a filter chosen this way would be fitted to the past. They are
recorded on every alert and every demo trade, so the demo account can confirm or
refute them on new data before any of them becomes a filter.

## Phase 10: session playbooks, A+ setups, monitoring before auto-trading

### D86. A playbook per session, chosen on two-thirds and checked on the rest

**The owner's request** (2026-11-21): trade the Asian (Tokyo), London and New York
sessions; spot A+ setups; find a strategy for each session by analysis and monitor
it before it auto-trades. Choices (all the recommended ones): the Silver Bullet
model is replaced by session playbooks; A+ is set from the evidence per session;
London and New York keep auto-trading the validated model meanwhile; a session's
candidate goes live after 20 monitored trades with a positive average and the
owner's `/approve <session>` (stage 2). The owner then asked for trend, structure
and further strategies to be researched and included.

**What was built (stage 1).**

- The Silver Bullet *model* is gone (its hours remain a confluence tag). Models:
  `sweep_mss`, `cisd`, `continuation`, and a new `breakout`: a session range
  (Asian, London, New York) or the previous day's high/low that breaks and holds
  (no close back inside within 3 bars) with a displacement candle; the trade
  follows the break, entered on the retest of the gap the leg left, stop beyond the
  leg's start, targets at the next liquidity. It reuses the sweep detector's "the
  level broke" verdict and the rest of the spec's pipeline.
- Trend and structure **context tags** (`analysis/context.py`), added after the
  engine from closed H1 bars (H4 and daily anchored to the 17:00 New York day):
  `d1_trend`, `h4_trend` (swing structure points the trade's way), `ema_trend` (the
  H1 200 EMA side), `weekly_value` (discount of the previous week for longs, premium
  for shorts), `high_vol` (H1 ATR over 1.2x its 120-hour median). The H1 swing
  structure was already in the score (bias) and the M5 structure in every setup.
- **Grades** (`analysis/grades.py`): A+ when a setup meets its session's candidate
  rule, A when it clears the alert threshold, B otherwise. Sessions for playbooks:
  asia, london, newyork (the overlap and post-news windows count as New York).
- **Playbook rules** (`config.yaml playbooks:`): per session a `trading` rule (what
  auto-trades now: London and New York the validated model at 60; Asia nothing) and
  a `candidate`. A rule: model, minimum score, `tags_any` (at least one), `min_tags`
  (a count of confluence tags), `tags_all` (every one, e.g. a trend).
- **The analysis** (`python -m goldsweep.playbook`): with setups allowed in the
  Asian session, each session x model is traded under 108 fixed rules (432 in all
  per session): score 60 / 70 / 80; none / Unicorn / Judas / either; 0 / 1 / 2
  confluence tags; no trend / daily / 4-hour / 200 EMA. A rule needs 15 trades in
  the first two-thirds of 2024-01-01..2026-10-31 (to 2025-12-01), is chosen there by
  R per trade (ties to fewer conditions), and must be positive both there and on the
  held-out last third, which played no part in choosing it; otherwise the session
  has no candidate (the runner-up is not tried).

**Research.** The sources (community and vendor material, one replication of an
academic opening-range study) give definitions but no reliable evidence: trend
filters on gold are reported without independent figures; one replication of an
opening-range breakout study found it profitable before costs and about zero
after; for gold, several describe London's break of the Asian range as a stop run
that reverses (what the sweep model trades). So everything was measured here.

**Results** (`reports/playbook/report.html`):

| Session | Candidate | First two-thirds | Held-out third |
|---|---|---|---|
| Asia (Tokyo) | none: the best rule (cisd, 1+ confluence tag, daily trend; +0.67 R per trade) lost on the held-out part | | |
| London | sweep + MSS, score 60+, **with Judas** | 16 trades, +1.50 R per trade | 10 trades, +1.06 R |
| New York | sweep + MSS, score 60+, **with Judas** | 16 trades, +0.70 R per trade | 8 trades, +1.31 R |

The trend conditions and the breakout model were tried everywhere but never
reached 15 trades in the choosing part with a better result: the rules that rank
highest on R per trade rest on one to four trades (a breakout rule in Asia: one
trade, +9.38 R; its one held-out trade lost), which is exactly what the 15-trade
minimum is there to ignore. A trend filter on the spec's model cut its sample below
the minimum, so on this history it cannot be judged. The same candidates came out
of the smaller first run (before the trend tags and the breakout model).

**Read with care.** 432 rules per session were tried, so the best is flattered even
after the held-out check; the held-out parts hold 8 to 10 trades. A candidate is a
hypothesis: it is monitored on new data before it may trade (stage 2), and the
London and New York candidates are subsets of what already trades there (the Judas
setups), so their monitoring is a check of the filter, not of new trades.

Across all sessions, the trades of today's trading rule (55, +0.93 R each) split by
the new tags: `h4_trend` 18 trades, +1.44 R; `weekly_value` 32, +1.20 R; `high_vol`
9, +1.37 R; `d1_trend` 15, +0.86 R; `ema_trend` 18, +0.45 R (worse than average).
Too few per session to choose on; they are recorded on every alert for monitoring.
The acceptance backtest with the shipped config is unchanged by this stage.

**Decision:** the candidates above are in `config.yaml` (Asia none); in this stage
they only label setups A+. `sessions.asia.trade` stays false until stage 2 routes
Asian setups to monitoring, so nothing new can trade before then.

### D87. Monitoring on a shadow book, then `/approve <session>`

**What was built (stage 2).**

- **Routing** (`live.py::_handle_signal`). A setup trades for real only when its
  session's *live rule* takes it: the approved candidate if there is one, otherwise
  the `trading` rule. The model's `models.<name>.trade` flag and the auto-trade
  threshold still apply on top (the safer reading: a rule never lowers them). A
  setup outside the rule is an alert: "alert only: outside the London playbook
  (...)", or "the Asia playbook does not trade yet".
- **The shadow book** (`execution/shadow.py`). Every alerted setup that meets its
  session's candidate, while that candidate is not approved, is also placed on a
  second `TradeBook`: the same fills, costs, deadline rule, management and risk
  limits as paper trading, on its own notional 10,000 balance, counted in R. It is
  never sent to MetaTrader 5, in paper or live mode. This happens whether or not the
  trading rule also took the setup: the London and New York candidates are subsets
  of what trades there, so this is how their filter gets measured. Finished trades
  go to the `shadow_trades` table (migration 6) with the session and the
  candidate's *fingerprint* (a hash of the rule). The book's state is saved after
  every change, so a restart carries on; paper and live share it (it never trades).
  The shadow book trades the plain plan, without M1 refinement (D83 is off).
- **Confirmation.** Finished monitored trades send no Telegram message (the owner
  chose fewer messages on reviewing this stage): each is logged, and `/status`
  shows every session's tally (finished, won, average R). When
  `playbooks.monitor_trades` (20) have finished with a positive average, one
  message asks for `/approve <session>` (once per candidate fingerprint). The alert
  for the setup itself still says that it is monitored.
- **Commands.** `/approve <session>` (asia/tokyo, london, newyork/ny) works only
  for a confirmed candidate; it stores `{session: fingerprint}` and from then on
  that candidate *replaces* the trading rule: only A+ setups trade in that session,
  as the owner asked. `/revoke <session>` returns to the trading rule and the
  candidate is monitored again. Editing a candidate in config.yaml changes its
  fingerprint: the approval no longer applies and the count starts from zero.
  `/approve` never arms an unattended start (`/golive` still does) and is not a
  dashboard command. `/status` lists each session: what trades there and the
  candidate's state and tally, and the monitored setups open now.
- **Grades in alerts.** Every alert has a "Grade:" line (A+ with its playbook, A or
  B) and, when monitored, "👀 monitored for the London A+ playbook (7 of 20
  finished)". Both are stored in the signal's detail and shown on the dashboard's
  Signals tab. Blocked setups follow `alerts.send_blocked` as before, monitored or
  not.
- **Safety.** The kill switch also clears the shadow book (nothing of it is
  counted) and no setup is monitored while trading is stopped; monitoring goes on
  while paused (it never trades). Risk limits of the real account count real trades
  only. Shadow-book errors are logged and never stop trading.
- **Asia is switched on** (`sessions.asia.trade: true`): the engine now looks for
  setups 09:00-15:00 Tokyo. Its playbook has no trading rule and no candidate, so its
  setups are graded and alerted, never traded, until an analysis finds a candidate
  that then passes monitoring.
- **Backtests trade the playbooks** (`backtest/engine.py`): a setup outside its
  session's trading rule is NOT_SELECTED ("outside the session playbooks"), and the
  walk-forward test sees only the setups the rules take. `--candidates` trades each
  session's candidate as if approved (sessions without one keep their trading rule),
  for comparison; the report lists the rules used and adds a by-playbook table.

**Results** (2024-01-01..2026-10-31, M1 fills, Asia now on):

| Run | Trades | Won | Profit factor | R per trade | Return | Max DD |
|---|---|---|---|---|---|---|
| Shipped config (trading rules) | 55 | 36.4% | 2.40 | +0.93 | +26.7% | 2.9% |
| `--candidates` (Judas rule in London and New York) | 49 | 40.8% | 2.87 | +1.17 | +29.9% | 2.9% |

The acceptance result is unchanged by switching Asia on (walk-forward: 45
out-of-sample trades, +0.89 R each): the engine found 87 Asian setups in the 34
months, about 2.5 a month, all recorded as not selected because Asia has no rule.
The `--candidates` figures are not new evidence: the Judas rule was chosen on the
first two-thirds of this same history (D86), so the comparison is flattered. That
is why a candidate must still finish 20 monitored trades on new data before the
owner is asked.

**Decision:** London and New York keep trading their current rule and monitor the
Judas candidate; Asia alerts only. No candidate trades without `/approve`.

## Scalper add-on (phases S0-S6)

The owner's prompt (2026-11-21) is `docs/SCALPER_SPEC.md`; the owner added "add this
alongside the trade system". The S0 review and plan is `docs/SCALPER_PLAN.md`. The
decisions below are S0's: where the prompt conflicts with the project as built, or
leaves something open, the safer option is taken. D89 needs the owner's choice.

### D88. Alongside, in the same bot, with its own identity at the broker

The scalper runs next to the sweep + MSS strategy, inside the same bot process: one
MT5 connection, one lock (the MT5 Python API is not thread-safe), one kill switch,
one `/golive`. It runs on M1 closes (a job each minute, as the M1 refinement job
does). It has its own magic number (the main one plus 1, configurable), its own
`TradeBook` state and its own trades table. Reason: the live executor closes any
position with its magic number that is not in its saved state, so shared numbers
would make each strategy close the other's trades.

### D89. Python decides and trades; MQL5 draws and has buttons (owner to confirm)

The prompt puts detection and execution in an MQL5 EA (`GoldScalper.mq5`) "for
speed", mirrored in Python and checked by a parity test. That conflicts with the
project's rules: only `execution/mt5_gateway.py` sends orders; one `TradeBook` serves
backtests, paper and live; unattended starts send nothing before `/golive`; the kill
switch closes everything from one place. An EA would bypass these and need a second
copy of every risk and trade rule, kept equal forever.

Signals are taken on closed M1 bars, so the EA's speed gains about one to three
seconds (the bot reads the bar just after it closes; the order then takes the
broker's ping). Exits do not need the EA's speed either: each child carries its
own broker TP and SL (D90).

**Recommended:** the scalper is Python (`goldsweep/scalping/`), the indicator only
draws what Python writes and turns its buttons into commands. There is then no
parity test and no Strategy Tester run of an EA; screenshots come from the demo
chart and from a replay, and the backtest is Python's (D95).

**Decision (owner, 2026-11-21):** Python, as recommended. S0 approved.

### D90. Broker-side targets for every child, not virtual ones

The prompt gives all three children the same broker TP (TP3) and has the EA close
child 1 at TP1 and child 2 at TP2. Here child 1 is sent with TP1, child 2 with TP2
and child 3 with TP3, all with the same SL, as the main strategy's three parts
already are (`plan_legs`, `MT5Executor`). The result is the same, and the targets
still work if the PC, the bot or the connection stops. `plan_legs` is generalised to
take a split list (40/30/30 for scalps; 50/30/20 stays for the main strategy).

### D91. One `scalper:` block, and limits across both strategies

The prompt's `scalping:` risk block does not exist; its settings and the prompt's
`scalper_v2:` block become one strict `scalper:` block with a `risk:` part. Starting
values, to be measured in S1 and S6: 0.25% per scalp (all three children together);
one active scalp; 8 scalps a day; scalper daily loss limit 1%; 3 losses in a row stop
it for the day; 300 s cooldown in the same direction after a stop-out; time stop 900
s (closed unless +0.3 R by then); spread at most 1.5x its median; TP1 at least 2x the
round-trip cost.

Across both strategies a new account guard applies before any new order: the
existing 2% daily and 5% weekly loss limits counted over both strategies' closed
trades, and at most 0.75% of equity at risk at once (0.5% main + 0.25% scalp). Each
strategy's own limits still apply.

### D92. No opposite positions; the main strategy comes first

Holding a scalp short while a main trade is long (or the other way) is a hedge,
which the project forbids. So a scalp is not opened against an open or waiting main
trade; and when a main order is about to be placed against an open scalp, the scalp
is closed at market first, because the main strategy is the validated one (D84) and
scalps are short-lived. Same-direction positions of the two strategies may coexist
within D91's combined cap. Beyond its three children the scalper never adds a
position (the stacking in the reference video is the anti-pattern).

### D93. The scalper starts on paper; the lock guards real money

Whatever `MODE` says, the scalper trades its own paper book on the live feed until
the owner sets `scalper.execution: broker` after S6's results. Then it sends orders
on demo accounts. On a real account it also needs `execution.allow_real_account`
and the file `data/scalp_forward_test_ok.json`, written only by a command the owner
types after reviewing at least four weeks of forward testing. Unattended starts stay
unarmed for the scalper as for everything else.

### D94. Two strategy modes are built, two wait for rules

`sweep_reclaim` (default) and `trail_rejection` have rules in the prompt and are
built in S1. `momentum_burst` and `asia_reversion` are named without rules; they are
not built and the strict config does not accept them until the owner describes them.

### D95. How scalps are tested

The project has about a million stored M1 bars (2024-2026). S1 replays them through
the scalp engine with costs and the worse case inside a bar, so a first verdict
(gross and net R, win rate against the breakeven win rate, trades a day, by session)
comes before anything runs live. S6 adds a tick replay for the period the broker's
tick history covers (size to be measured), walk-forward, the `tight` and `wide`
ladders, and the forward test. Entries are modelled one tick after the bar closes
plus the bot's delay.

### D96. The assistant reads the database and never trades

The assistant service (`python main.py --assistant`, FastAPI on 127.0.0.1:8765, a
token from `.env`) is a separate process like the dashboard: it reads SQLite (the bot
writes a scalper snapshot each minute and the M1 candles) and never connects to MT5.
Quick commands are computed locally and timed. Free text goes through
`LlmChat.complete()` (the daily budget applies; the provider is `LLM_PROVIDER`, Groq
today, not Claude Haiku as the prompt assumes). "Quick Scalp (Manual)" only proposes;
its confirm button writes a `commands` row that the bot carries out through the
scalp executor with every check, and nothing while unarmed. The LLM is never on the
order path.

### D97. Quiet Telegram by default

Scalps can come several an hour. Defaults: one message when a scalp is entered (with
its box), one when it closes (net money and R), and the daily summary. Child fills,
TP hits and every trail step are off by default (`scalper.alerts` switches), in line
with D87. New commands: `/scalp on|off` (the scalper only), `/trail on|off`, `/stats`.
The chart picture's renderer is chosen in S5 (Plotly's image export needs extra
software).

### D98. GoldSweep's own look; the reference material stays out

The visuals follow the prompt's section 3 (charcoal background, gold accents, the
text "GoldSweep" with a small diamond, no mascot or artwork). The reference
screenshots and videos show a third-party commercial product: they are not copied
into the repository (`reference/` is ignored by git), and a test checks that the
product's name appears in no code, MQL5 file or UI text. The videos could not be
played here; the owner's written analysis of them and the three screenshots were
used.

### D99. The chart indicator writes only a command file

`GoldSweepPanel.mq5` v2 draws the scalper (tags, step line, X markers, twin bars,
boxes, dashboard) from a `scalp.json` the bot writes, and its buttons (Adaptive
Trail, Pause) write `scalp_commands.json`, which the bot reads like dashboard
commands. It never trades. It shows STALE when the file stops updating. Symbol
handling stays generic, with gold the only symbol.

### D100. S1: the scalper's rules where the prompt left them open

Built in `goldsweep/scalping/` (pure, one closed bar at a time) and in the shared
trade book (`Management`, `breakeven_plus_stop`, `ladder_stop`, `step_trailed_stop`,
`time_stop_due`; a book without `Management` behaves exactly as before):

- **Step trail:** hl2 +/- 2 x ATR(10) (simple average, as the project's ATR), the
  trend flips on a close through the line in force during the bar.
- **Micro levels:** fractal swings with 2 bars each side (the main engine's rule),
  usable for 15 bars from the pivot; twin bars within 0.05 ATR(14); the Asian and
  London ranges once their window has ended (12 hours, or not at all if no bar came
  within an hour of the end). A sweep needs 0.05 ATR beyond the level and a close
  back inside on that bar or the next; a level is used once.
- **`sweep_reclaim`:** trend down (or flipping down), a high swept within 6 bars, and
  a bearish close below the sweep candle's low; one setup per sweep.
  **`trail_rejection`** (the prompt gives no details): trend down and not flipping,
  the bar's high touches the line in force while its body stays below, and a bearish
  close below the previous bar's low. Mirrored for buys.
- **Stop:** the X (or the rejection wick) plus 0.1 ATR (plus the spread for a short);
  if that is outside 0.5-3 ATR from the entry, the step line is tried; else no trade.
  **Ladder** 0.5 / 1.0 / 1.5 R, widened up to 2 R steps if TP1 is not 2x the round-trip
  cost away, else no trade. Market entry at the next bar's open.
- **Children** 40 / 30 / 30 with their own broker targets (D90). **Adaptive trail:**
  after TP1 the stop goes to the entry plus slippage and commission ("breakeven+"),
  after TP2 to TP1, and from the fill it follows the step line +/- 0.1 ATR; every move
  only tightens. **Time stop:** one look 15 minutes after the fill: below +0.3 R and
  not at TP1, it is closed.
- **H1 filter:** the H1 structure bias (no news, no previous-day location in the
  backtest) must allow the side; neutral allows both. The score (60, +10 for a
  twin-bar or session-range level) is a label, not a filter.

### D101. S1 verdict: the scalper loses after costs (2024-2026)

`python -m goldsweep.scalping.backtest --from 2024-01-01 --to 2026-10-31`: 1,009,251
stored M1 bars, 53,651 setups of both modes, every variant traded with the scalper's
limits (0.25% a scalp, 8 a day, 1% daily loss, spread at most 1.5x its median) and the
default costs, then again without costs:

| Variant (M1) | Trades | A day | Won | Breakeven win rate | R per trade | Without costs |
|---|---|---|---|---|---|---|
| sweep_reclaim, H1 bias, 0.5 R (configured) | 4,447 | 5.0 | 53.7% | 69.1% | -0.20 | -0.02 |
| sweep_reclaim, no filter, 2 R | 3,703 | 4.2 | 32.1% | 38.7% | -0.13 | -0.01 |
| trail_rejection, H1 bias, 2 R | 1,025 | 1.2 | 29.2% | 35.8% | -0.16 | +0.04 |
| trail_rejection, no filter, 2 R | 1,356 | 1.5 | 30.0% | 35.7% | -0.14 | +0.06 |

All eight M1 variants lose after costs (-0.13 to -0.22 R a trade; the configured one
would have lost 88% of the test balance). Before costs they are flat (-0.04 to +0.06
R): the setups themselves show no edge, and the costs (spread, slippage, commission)
take 0.1-0.2 R from every trade, because a one-minute stop is only a few dollars.
The same rules on M5 bars lose less but still lose in every variant (-0.04 to -0.15 R
after costs, -0.05 to +0.06 R without). No session, direction, level kind, hour or
year is positive in a way that is more than noise.

**Read with care.** Bars only: inside a bar a stop and a target count as the stop, which
is harsh at one minute; a tick replay (S6) would remove that, but it cannot remove the
costs, which alone exceed every gross result measured. There is no historical news
calendar. Results on the past do not predict the future in either direction.

**Decision:** the scalper stays off (`scalper.enabled: false`) and is not wired into
the live bot. S2-S6 wait for the owner's decision on this result. The code, tests and
report stay, so any change to the rules can be measured the same way.

### D102. Research on fast, intraday and swing scalper modes (2026-11-22)

The owner asked for research on scalper robots that open many trades and close them
within seconds once price moves their way ("HFT mode"), on intraday and swing modes
and on other modes. The findings, sources and measurements are in
`docs/SCALPER_MODES.md`. In short: retail "HFT" gold robots are tick scalpers whose
results depend on spread and latency and are published only by their sellers; latency
arbitrage is forbidden by the broker; seconds-scale edges on exchanges come from order
books, which XAUUSDm does not offer; gold absorbs news in about 90 seconds, which the
news blackout and widening spreads rule out here; the opening-range breakout is about
zero after costs in an independent replication; trend following over weeks to months
is the best-documented pattern. Measured here: 69 ms ping, a 0.24 spread that is 36%
of a typical 10-second move, no commission on this account, two weeks of tick history,
daily bars from 2014, swap of -43 USD a lot a night on longs.

### D103. One scalper, three modes, a switch, evidence before trading

The scalper gets three modes, each with its own entries and tools (the keep/drop table
in `docs/SCALPER_MODES.md`): **tick** (the "HFT" request, honestly named: seconds to
two minutes, one trade at a time, many a session), **intraday** (flat by the New York
close) and **swing** (days to weeks, swap paid). `/mode main|scalper|both` chooses which
strategies may trade; `/scalper off|tick|intraday|swing` chooses the scalper's mode;
each mode is off, paper (shadow book) or trading. A mode may trade only after its
evidence gate (positive after realistic costs on data it was not tuned on, enough
trades, positive held-out third, 20 positive paper trades) and the owner's
`/approve scalper <mode>`. The kill switch stops every strategy.

### D104. Never built, whatever the switch says

Latency arbitrage (broker terms); news-spike trading (blackout rule, spreads,
slippage); grid, martingale, averaging down, hedging to recover (ground rules);
stacking positions beyond one scalp's three children in one risk budget; order-flow
strategies (no order book). "Many trades" means many in a row, one at a time.

### D105. The tick mode runs in its own process

A tick loop deciding several times a second cannot wait behind the bar job, which
holds the bot's lock for seconds while it reads the broker and runs the engines. So the
tick mode runs as its own process (`python main.py --scalper`) with its own MT5
connection, magic number and state; the account guard reads every GoldSweep position
from the broker; the main bot's kill switch closes the scalper's positions too and
writes a stop the scalper obeys within a second; unattended starts stay unarmed. This
amends D88 for the tick mode only. It also needs a Windows VPS with a ping under 20 ms
to the broker (the MQL5 VPS cannot run Python); without one the tick mode stays on
paper.

### D106. Ticks are recorded from S2 on

The broker serves about two weeks of tick history, too little to judge a tick mode.
From S2 the bot saves every XAUUSDm tick (time, bid, ask) in daily compressed files
under `data/ticks/`. The tick mode is judged when at least 8 weeks are recorded.

### D107. Costs as this account charges them (owner to confirm)

The backtests charge 7.00 a lot in commission (the spec's ECN-style default), but our
122 demo deals on XAUUSDm (an Exness standard account) paid none; its cost is the
0.24 spread. Proposed: the cost model per account, commission 0 for this one,
measured from the broker's deals. This changes the acceptance backtest's figures (it
makes every result a little better), so it waits for the owner's approval. With no
commission the S1 scalper still loses in all eight M1 variants: -0.10 to -0.18 R a
trade (it was -0.13 to -0.22 R), the configured one -0.14 R over 4,631 trades; before
costs they stay at -0.04 to +0.06 R. D101's verdict stands.

### D108. S2: the switch, the tick recorder, latency and costs (built)

The owner approved the plan of D102-D107 and its cost change (2026-11-22).

- **Switch.** `switch:` in config.yaml (`strategies: main`, `scalper_mode: "off"`; YAML
  reads a bare `off` as false, which is taken as "off") gives the startup defaults.
  `/mode main|scalper|both` and `/scalper off|tick|intraday|swing` change them while the
  bot runs (stored as `switch.strategies` / `switch.scalper_mode`, shared by paper and
  live like approvals); the dashboard's Control tab sends the same commands. With
  `/mode scalper` main setups are alerts only ("the main strategy is switched off");
  monitoring of A+ candidates goes on. `scalper.enabled` is gone: the switch decides.
  `scalping/modes.py` holds each mode's state: none is built yet, so selecting one says
  "not built yet (phase S3/S4/S5): nothing runs", and `/approve scalper <mode>` refuses
  until the mode is built and has passed. `/status` and the dashboard snapshot show it.
- **Tick recorder.** `BrokerClient.get_ticks` (MT5: `copy_ticks_range` with bid/ask
  changes, server time converted to UTC like bars; the mock: four ticks a minute). The
  bot's `tick_job` (each minute at :30, under the trading lock) saves each finished
  hour, a minute after it ends, to `data/ticks/<symbol>/<YYYY-MM-DD>/<HH>.npz`; on the
  first start it fetches the broker's last 14 days, 12 hours a call; a broker error
  retries the same hour; hours without ticks leave no file; `ticks.keep_days` (0 =
  keep all) prunes. Checked read-only on the real terminal: 36,413 ticks in Friday
  13:00-14:00, about 0.3 MB an hour.
- **Latency.** `TerminalInfo.ping_ms` (MT5's `ping_last`) is shown by `--check` and
  `/status`; every `order_send` is timed and stored in `order_log.elapsed_ms`
  (migration 7); `/status` shows the median of the last 20.
- **Costs.** `backtest.costs.commission_per_lot: 0.0` in config.yaml (also used by paper
  trading and the shadow book); the built-in default stays 7.00 for other accounts.
  Tests pinned to the old figures now read +228.70 / +4.79 R for the textbook trade.
  Acceptance backtest, 2024-01-01..2026-10-31: the same 55 trades, 36.4% won, profit
  factor 2.43 (was 2.40), +0.94 R a trade (was +0.93), +27.1% (was +26.7%), max
  drawdown 2.9%; walk-forward 45 out-of-sample trades at +0.90 R (was +0.89).

### D109. S3: the intraday mode fails its evidence test

**Built** (`goldsweep/scalping/intraday.py`, `intraday_backtest.py`, config
`scalper.intraday`): three rules fed one closed M5 bar at a time, every position flat
by the New York rollover through a new shared rule (`TradePlan.close_by`,
`close_by_due`, exit reason `session_end`; plans without it, the main strategy's, are
unchanged):

- **Asian breakout:** in the London window, the first close beyond the Asian range
  (by 0.1 ATR) trades that way; stop beyond the range's middle; one a day; the range
  must cover at least half its window.
- **New York opening range:** the high and low of 09:30-09:45 New York; the first close
  beyond it until 12:00 New York; stop beyond its middle; one a day.
- **VWAP trend:** a tick-volume VWAP from the London open; in the London and New York
  windows a close across it (by 0.1 ATR) with the VWAP moving that way over 6 bars;
  stop beyond the last 6 bars' extreme, then the VWAP trails it; one per window and
  direction a day.
- Stops must sit 0.5-4 ATR(M5) away; ladders 1/2/3 R and 0.5/1/1.5 R; children 40/30/30;
  breakeven+ at TP1, the stop to TP1 after TP2; the scalper's risk limits; trades
  managed on M1 bars. The gate (D103): the ladder chosen on the first two-thirds must
  be positive on the held-out third, positive overall after costs, with 100+ trades.

**Results** (2024-01-01..2026-10-31, `reports/intraday_2024-01-01_2026-10-31/`):

| Rule (chosen ladder) | Trades | Won | R per trade | First 2/3 | Held-out 1/3 | Without costs |
|---|---|---|---|---|---|---|
| Asian breakout (0.5/1/1.5 R) | 121 | 60.3% | -0.10 | -0.07 (87) | -0.18 (34) | -0.08 |
| Opening range (0.5/1/1.5 R) | 686 | 64.7% | -0.06 | -0.07 (464) | -0.04 (222) | +0.00 |
| VWAP trend (0.5/1/1.5 R) | 770 | 45.3% | -0.06 | -0.07 (506) | -0.05 (264) | +0.01 |

Every variant loses after costs (-0.06 to -0.14 R a trade) and is flat before them.
The Asian breakout loses even before costs, and 332 of its 459 setups needed a stop
beyond 4 ATR because the range's middle is far away: London's break of the Asian
range tends to reverse, the very raid the main sweep + MSS strategy trades. The VWAP
trend's trailing exits average -0.28 R. These are fixed, textbook rules; tuning them on
the same history would only fit its noise, so nothing was tuned.

**Decision:** the intraday mode does not run; `scalping/modes.py` says so on `/scalper
intraday` and refuses `/approve scalper intraday`. Next: S4, the swing mode, on daily
bars from 2014.

### D110. S4: the swing mode fails its evidence test, and cannot be sized here

**Built** (`goldsweep/scalping/swing.py`, `swing_backtest.py`, config `scalper.swing`):

- **Swap.** `SymbolSpec` now carries the broker's swap terms (`swap_mode`, `swap_long`,
  `swap_short`, `swap_rollover3days`; `swap_per_lot`), read by the MT5 client. The trade
  book charges it per child for each night held when `Management.swap` is on
  (`swap_nights`: one per 00:00 UTC rollover after a weekday, three after Wednesday,
  none after the weekend; `Trade.swap`, included in the result). XAUUSDm today: longs
  pay 431.6 points (43.16) a lot a night, shorts nothing. Main-strategy backtests do not
  charge swap, as before (their trades rarely stay overnight; a stated limitation).
- **History.** The broker's H4 bars since 2014-01-13 (16,561), downloaded once,
  read-only, with the symbol facts refreshed (`swing.h4_fetched_from` remembers it);
  daily bars are UTC days built from them, weekend stubs left out.
- **Rules**, one closed day at a time: Donchian breakout (a close beyond the last 55
  days), 12-month momentum (decided on each month's first day, closed when the next
  month begins through `close_by`), pullback (above the 200-day average with the
  50-day above it, a close back across the 20-day average; mirrored). First stop 2
  daily ATRs; ladders 2/4/6 R and 3/6/10 R; breakeven+ at TP1, the stop to TP1 after
  TP2; breakouts and pullbacks trail the last 20 days' low (high) through a per-side
  line (`line_for`); managed on H4 bars; swap every night. Same gate as D109 with 30+
  trades.

**Results** (2014-01-01..2026-10-31; `reports/swing_2014-01-01_2026-10-31*`):

- **Size.** The median first stop of the last year is 189 an ounce, so the minimum
  0.01 lot risks 189: 0.25% a trade needs a balance of about 76,000. At the test's
  10,000, 205 of 257 breakout setups, 119 of 141 momentum ones and 136 of 157 pullbacks
  could not be sized (none in the last four years); on this 1,071 account one minimum
  trade would risk about 18%.
- **Edge, measured at a notional 1,000,000** so that the minimum lot does not decide:

| Rule (chosen ladder) | Trades | R per trade | First 2/3 (2014-2022) | Held-out 1/3 (2022-2026) | Without costs |
|---|---|---|---|---|---|
| Donchian breakout (2/4/6 R) | 66 | +0.10 | -0.04 (41) | +0.32 (25) | +0.22 |
| Momentum (2/4/6 R) | 136 | +0.03 | -0.17 (86) | +0.38 (50) | +0.10 |
| Pullback (2/4/6 R) | 69 | +0.03 | -0.27 (45) | +0.60 (24) | +0.19 |

All three are slightly positive overall, but only because of the held-out years, gold's
2022-2026 rise from about 1,700 to over 4,000; over 2014-2022 every rule lost. Swap took
a large share (about 19,000-32,000 on the notional million). A rule that only works in
one strong trend is the trend, not an edge the gate can accept.

**Decision:** the swing mode does not run (`modes.py` says why); with S1 (D101) and S3
(D109) every scalper mode tested so far has failed. The tick mode (S5) needs 8 weeks of
recorded ticks and a VPS under 20 ms; the recorder runs since 2026-11-22.

### D111. Scalper v3 and S5: the HFT mode on M1, tested on paper and on the demo account

**The owner's goal (2026-11-22):** a scalping bot with an HFT mode on the one-minute
timeframe in the active sessions, and intraday and swing-scalping modes; the tick
recorder set aside; every approved build tested on paper and on the demo account.

**Decisions:**

- **Backtests are reported, not gates.** D103's evidence gate no longer stops a built
  mode: it runs on the scalper's paper book when selected, and `/approve scalper <mode>`
  sends it to the demo account. The backtests stay, and their results are shown beside
  the paper and demo results so the owner judges both. Real money stays locked
  (`execution.allow_real_account` and `data/scalp_forward_test_ok.json`, D93).
- **Timeframes** (measured on the last 12 months; stop 1.5 ATR, balance a 0.01 lot needs
  at 0.25%): M1 3.10 (1,241), M5 7.23 (2,893), M15 12.97 (5,188), H1 27.45 (10,980), H4
  54.39 (21,755), D1 128.71 (51,484). So: **HFT on M1**, **intraday on M5** (entries and
  management on M1) and **swing scalping on M15** (managed on M5), S6. Daily or H4 swing
  stays out (D110). A 10,000 demo account is recommended for testing all three; the
  scalper's paper book is a notional `scalper.paper_balance` (10,000).
- **Renamed** `tick` to `hft` (config literal, `modes.py`, dashboard, commands); the HFT
  mode works on closed M1 bars, so it needs no tick history. `ticks.record` defaults
  to false (config and built-in); the recorder code stays.
- **The HFT mode** trades S1's two rules together (`sweep_reclaim` and
  `trail_rejection`) with the ladder 0.5/1/1.5 R, breakeven-plus at TP1, the stop to TP1
  after TP2, the step trail and the 15-minute time stop. It drops the H1 bias filter
  (`htf_filter` now only names S1's backtest variants) and adds an **activity filter**:
  a scalp only while the M1 ATR is at least `activity_min` x its median over the last
  `activity_bars` bars (1.0 x a day), because a quiet market cannot pay the spread.
- **Executors.** `PaperExecutor` and `MT5Executor` take a state key, a trades table, a
  `RiskConfig` and a `Management`; the defaults are the main strategy's, unchanged. The
  scalper's paper book is `scalper.paper.book` (table `scalp_trades`); its demo executor
  is `scalper.live.book` (table `scalp_live_trades`) on a gateway with
  `scalper.magic_number`, comments `GS-SC|<id>|<n>`. Migration 8 adds both tables.
- **`MT5Executor` applies `Management`** with the trade book's functions
  (`breakeven_plus_stop`, `ladder_stop`, `step_trailed_stop`, `time_stop_due`,
  `close_by_due`): stops move through `gateway.modify`, the time stop and the flat-by
  time close at market through `gateway.close` (not while unarmed). The breakeven-plus
  stop is each position's own fill plus the offset.
- **The account guard** (`risk/account_guard.py`) checks a demo scalp against the
  account: the realised result of both strategies today and this week against the
  account's 2% and 5% limits, at most `scalper.risk.max_open_risk_pct` (0.75%) open risk
  across both, and **no opposite positions** (no hedging). The main strategy comes
  first: before a main order, an opposite demo scalp is closed (`Trader._yield_scalps`).
  Paper scalps are a separate notional book and are not guarded against the main one.
- **The runner** (`scalping/runner.py`), run each minute by `Trader.scalper_job` under
  the bot's lock, whatever the mode: warms the engine on 600 M1 bars, feeds each new
  closed bar to the engine and to both scalper executors (with the step line), and
  checks a setup on the newest bar: mode selected and built, `/mode` scalper or both,
  trading active, not older than 90 seconds, one scalp at a time, an active session,
  the activity filter, the 5-minute cooldown after a stop-out, the stop and cost gate
  (`scalp_plan`), the account guard (demo), then the executor's own checks (limits,
  spread, news blackout). Ids are `<date>-HFT-<n>`. Messages are two per scalp
  (placed, closed, D97). The kill switch closes scalps on both books; `/golive` arms the
  scalper's demo executor too; `/status` and the dashboard snapshot show the mode,
  where it trades, open scalps and the last 24 hours.

**The HFT backtest** (the `hft` line added to `python -m goldsweep.scalping.backtest`,
2024-01-01..2026-10-31, costs of this account):

| Variant | Trades | A day | Won | Break-even win rate | PF | R a scalp | Before costs | Total R | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| HFT: both rules, no H1 filter, active only, 0.5 R | 3,760 | 4.25 | 56.0% | 65.3% | 0.64 | -0.12 | -0.02 | -438.7 | 65.8% |
| S1 `sweep_reclaim`, no H1 filter, 0.5 R (for comparison) | 4,667 | 5.28 | 55.2% | 65.8% | 0.61 | -0.13 | -0.02 | -624.0 | 78.0% |

The activity filter skipped 7,348 setups and helped a little; the HFT mode still loses
about 0.12 R a scalp after costs and is flat before them, in every session, direction
and year (2024 -0.15, 2025 -0.10, 2026 -0.09). At 0.25% a scalp that is roughly -0.13%
of the scalper's balance a trading day, before its 1% daily limit. The owner chose to
test it on paper and on the demo account anyway; this is the figure the paper and
demo results will be compared with, and nothing suggests live results will differ
for the better. Report: `reports/scalper_2024-01-01_2026-10-31/`.

**Status:** S5 built and tested; the HFT mode runs on paper once selected
(`/mode both`, `/scalper hft`) and on the demo account after `/approve scalper hft` and a
bot restart. Results are not promised either way. S6 (intraday M5, swing scalping M15)
and S7 (chart panel, Scalper tab, `/stats`) follow after approval.

### D112. S6: the intraday (M5) and swing-scalping (M15) modes

**Built** (`goldsweep/scalping/logic.py`, `bars.py`, `swing_scalp.py`, `mode_backtest.py`;
config `scalper.intraday.mode_*`, `scalper.swing_scalp`, `scalper.h1_trend`):

- **One interface for every mode.** `ModeLogic` (HFT, intraday, swing) is fed closed
  M1 bars; each mode builds its own signal bars (`BarAggregator`: M5, M15) and the H1
  trend from them, returns `Candidate`s, filters them (`gate`) and makes the order
  (`plan`, always with SL and TP). The live runner and the new mode backtest use the
  same objects in the same order (bar to the mode, bar to the book, then the gates),
  so a backtest sees what the bot would have seen.
- **The H1 trend** (`scalper.h1_trend`): the last H1 close beyond a 50-hour EMA that is
  higher (lower) than 5 hours earlier; otherwise flat. Built from the M1 feed, so the
  runner warms up on 6,000 M1 bars.
- **Intraday on M5**, entries and management on M1, every position flat by the New York
  rollover (`close_by`): the S1 sweep and reclaim run on M5 bars (micro and session
  levels), plus S3's New York opening range and VWAP trend; not against the H1 trend.
  Stops 0.5-4 ATR(M5) beyond the anchor; ladder 1/2/3 R; breakeven+ at TP1 and the stop
  to TP1 after TP2. Left out on the evidence of D109: the Asian breakout (it lost even
  before costs) and the VWAP trailing stop (its exits averaged -0.28 R).
- **Swing scalping on M15**, managed on M1 bars (finer than the plan's M5, and the same
  in the backtest and live): with the H1 trend only, an **EMA pullback** (a touch of the
  M15 20 EMA within 3 bars, then a close back with the trend beyond the previous bar)
  and a **range breakout** (the first M15 close beyond the day's Asian or London range
  after its window). Stops 0.5-3 ATR(M15); ladder 1/2/3 R; breakeven+ at TP1, the stop to
  TP1 after TP2, then the stop trails the last 20 M15 bars' low (high); swap charged;
  closed after 2 days at most and an hour before the Friday close.
- **Books per mode.** Each mode has its own paper book (`scalper.<mode>.paper.book`; HFT
  keeps `scalper.paper.book`) and, on a live bot, its own demo executor with its own
  magic number (`scalper.magic_number` +0 HFT, +1 intraday, +2 swing; the config
  refuses a main magic number among them) and comment (`GS-SC`, `GS-SI`, `GS-SW`). The
  trades tables are shared; ids carry the mode (`HFT`, `INT`, `SWS`). Every mode is fed
  every minute, so a switch is immediate and a mode switched away from still manages
  its open scalp to the end; only the selected mode opens new ones. The account guard
  counts every scalper mode's demo positions. All three modes are built
  (`modes.py`).
- **The mode backtest**: `python -m goldsweep.scalping.mode_backtest --mode
  intraday|swing|hft --from ... --to ...` (with and without costs; first two-thirds and
  last third; by rule, session, direction, exit and year). Reported, not a gate (D111).

**Results** (2024-01-01..2026-10-31, a 10,000 balance, this account's costs;
`reports/scalper_<mode>_2024-01-01_2026-10-31/`):

| Mode | Trades | A day | Won | PF | R a trade | Before costs | First 2/3 | Last 1/3 | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| Intraday (M5) | 1,388 | 1.57 | 49.7% | 0.92 | -0.04 | +0.01 | -0.02 | -0.08 | 19.5% |
| Swing scalping (M15) | 825 | 0.93 | 46.8% | 0.86 | -0.07 | -0.04 | -0.06 | -0.08 | 13.3% |
| HFT (M1, D111) | 3,760 | 4.25 | 56.0% | 0.64 | -0.12 | -0.02 | | | 65.8% |

- **Intraday** is the nearest to break-even of every scalper mode so far: flat before
  costs, -0.04 R after them; New York -0.00, London -0.08; by rule, sweep and reclaim
  -0.04 (917), opening range -0.01 (201), VWAP trend -0.07 (270); 2024 +0.04, 2025 -0.12,
  2026 -0.05. 1,692 setups were against the H1 trend.
- **Swing scalping** loses before costs too; the pullback -0.05 (727), the range
  breakout -0.19 (98). At 10,000 an M15 stop allows about 0.02 lots, so a scalp often
  cannot be split into three children; 86 setups could not be sized at all.
- **Account size.** On the current 1,071 demo account 0.25% is about 2.68: the minimum
  0.01 lot risks about 3.10 on a median M1 stop, 7.23 on M5 and 12.97 on M15. Sizing
  never rounds up, so the demo executor refuses many HFT scalps and nearly every
  intraday and swing one there ("no size"); the paper books (a notional 10,000) do not.
  A 10,000 demo account (D111) is needed before `/approve scalper <mode>` can trade them.

Nothing was tuned on this history. **Decision:** S6 is built; each mode runs on paper
when selected and on the demo account after `/approve scalper <mode>`; the owner judges
them on those results against these figures. Next: S7 (chart panel, Scalper tab,
`/stats`).

### D113. S7: the scalper on the chart, the dashboard and Telegram

**Built** (`goldsweep/scalping/stats.py`, `chart.py`; `mt5/GoldSweepPanel.mq5` v2; the
dashboard's Scalper tab; Telegram `/stats`):

- **Results per mode** (`scalping/stats.py`), one source for every view: the stored
  scalps of both tables, the mode read from the id (`HFT`, `INT`, `SWS`), grouped by
  mode and account (paper, demo); trades, won, the **break-even win rate** (the win rate
  at which the average win and loss cancel), profit factor, R each and in total, net,
  and each mode's backtest R a trade (D111, D112) beside it. After costs.
- **`/stats [days]`** on Telegram: those lines for the last 30 days (or 1-365), plus the
  main strategy's. Also in `/help`.
- **The dashboard's Scalper tab**: mode, where it trades, the paper books, open scalps,
  the results table for 1/7/30/90 days, the running total R per mode and account, and
  the closed scalps. The bot's snapshot now carries every mode's paper balance and
  waiting scalps; position summaries carry the plan as placed (`stop_loss`,
  `take_profits`, `fill_time`), for the chart.
- **`scalp.json`** (`ScalpPanel`), written beside `signals.json` after every scalper
  minute when the chart panel is on, in the same line-per-item format: a flat head
  (mode, PAPER/DEMO, status READY / IN TRADE / PAUSED / OFF, trend and ATR on the mode's
  bars, session, spread and median, the next news release, the active scalp, today's
  and 30 days' results; `updated_gmt` in true UTC), then the HFT step line (180 M1
  points), sweep "x" markers (the engine now keeps its recent sweeps), each scalp of
  the last 24 hours and every open one, and TP-hit markers from the stored exits.
- **Panel v2** draws those in the GoldSweep look (charcoal box, gold header with the
  diamond, green BUY, red SELL, dashed TPs) beside the main signals, which it still
  draws as before; every part can be hidden from the inputs. It shows **STALE** when
  `updated_gmt` is more than three minutes old. Compiled in MetaEditor: 0 errors, 0
  warnings. It was not attached to a live chart here (no screenshots can be taken
  from this session); the owner attaches it after copying it to `MQL5\Indicators`.
- **Buttons** (D99): Pause / Resume and Scalper off. A click writes one line,
  `{"id", "t", "cmd"}`, to `scalp_commands.json`; the bot's dashboard job (every 2 s)
  carries out a command once (a higher id than the last, kept as state), only if at
  most two minutes old and one of `pause`, `resume`, `scalper:off`, and reports it on
  Telegram ("📊 Chart /pause: ..."). Narrower than D99 on purpose: the **Adaptive
  Trail** toggle is left out (a mode's management is fixed while its scalps are open;
  changing it mid-trade would make paper, demo and backtest disagree), and so is the
  kill switch (one click on a chart is too easy to make by accident; it stays on
  Telegram and the dashboard with its confirmation). The indicator never trades; that
  file is the only thing it writes.

**Not built** from the original add-on prompt, and why: the in-terminal assistant
(`GoldSweepAssistant.mq5` and its local service, prompt section 6) is left for a later
phase if the owner wants it; Telegram chart snapshots (PNG) are not sent, the
dashboard shows the same picture; "twin bars" brackets and trend lines are not drawn
(the engine uses twin-bar levels; the step line and the boxes carry the trade).

### D114. S8: chart panel v3, the trading picture inside MetaTrader 5

**The owner's request (2026-11-22):** make GoldSweep configurable inside MT5 like the
reference chart they sent (the third-party product of D98), with design, strategy and
order indicators on the chart, chart analysis, every upcoming signal and above all A+
setups, and steps to load it.

**Branding:** the reference's layout and behaviour are followed: the dark chart, BUY/SELL
tags, the trade box with right-aligned SL / ENTRY / TP1-TP3 labels and dashed TPs, the
stepped trail, the "x" at the sweep, TP-hit marks, the dashboard and a settings window.
Its robot artwork, purple theme, emblem and name are not used (D98). The look is
GoldSweep's (prompt section 3), with a small "◆ GoldSweep" mark.

**Built:**

- **Setups forming** (`SignalEngine.forming(trigger)`): every pool the engine has pierced,
  swept or shifted but that is not a signal yet, with its side, stage, since when, the
  raid's extreme and whether it went beyond the New York midnight open (the Judas
  reference, now `ny_midnight_open`, shared with the signal's own tag). Read-only.
- **"A+ possible"** (`grades.a_plus_possible`): the current session's candidate rule can
  still be met. Only what is known before the signal counts: the model and Judas. A rule
  that needs a tag the raid cannot show yet (SMT, a trend tag) gets no "possible", so
  the chart never promises more than it knows. It is a watch mark, never an order.
- **`signals.json` v2** (`execution/panel.py`): signal lines keep every v1 key and add
  the grade, model, tags, playbook, the A+ monitoring flag, the sweep's extreme and
  time, the statuses armed and closed, and the result. New lines are `"k":"forming"`
  (A+ possible or not), `"k":"pool"` (the four nearest unswept pools above and below)
  and `"k":"fvg"` (open gaps, `analysis.zones.open_gaps`, moved from the dashboard).
  The head carries the analysis: H1 bias, news bias, pressure, session and window, next
  release, approved playbooks, the main strategy's last 24 hours, and the latest
  scenario brief. Text is plain ASCII without quotes: dashes and quotes are converted,
  accents dropped.
- **`scalp.json` v3** (`scalping/chart.py`): every mode's layer, tagged with its mode:
  the HFT step line and sweeps; intraday VWAP, M5 sweeps and the Asian and NY opening
  ranges; swing M15 EMA, the 20-bar channel and the Asian and London ranges
  (`ChartMarks.lines` and `boxes`, small `ranges()` accessors on the engines). It also
  carries the selected mode's latest decision (`ModeSlot.decision`: placed, skipped and
  why, or no setup; the "Quick Scalp" line) and one status line per mode. On weekends
  the session reads "closed" (the Tokyo window "opens" on Sundays).
- **Indicator v3** (`mt5/GoldSweepPanel.mq5`, compiled in MetaEditor: 0 errors, 0
  warnings):
  - everything above, in grouped inputs (Files, Chart theme, Main strategy, Scalper,
    Panels, Alerts, Colours);
  - the lowest grade drawn: A+ only / A and A+ / all;
  - mode layers: the selected mode / all / none;
  - an analysis box with Market, Levels, Setups, Scalper, Today and Brief pages,
    answered from the files, with no AI call;
  - the dashboard with the Quick Scalp line;
  - the chart theme, saved per chart in terminal global variables and restored when
    the indicator is removed;
  - an alert once per new A+ signal and per new "A+ possible" setup, by pop-up, sound
    and an optional phone push; nothing is alerted for what is already there when it
    loads.
- **Buttons** (`PANEL_COMMANDS`): pause / resume, `mode:main|scalper|both`,
  `scalper:off|hft|intraday|swing`, the same switches as Telegram, each carried out once
  and only within two minutes. After a click the buttons show the state just asked for,
  so a second click on Pause sends Resume (v2 sent Pause four times in a row on the
  owner's chart). A `scalp.json` written before the bot read the click does not undo
  that; the first copy written 5 s or more after it, or any copy after 3 minutes, has
  the last word. The kill switch, `/approve` and every risk number stay off
  the chart: a chart setting can never raise risk. The unanswered question of what MT5
  may configure took this safer side (CLAUDE.md); parameters such as risk % can be added
  if the owner asks.
- **`deploy/install-panel.ps1 [-DryRun]`**: finds every MT5 data folder (with
  `origin.txt`), keeps the installed panel as `GoldSweepPanel.prev.mq5`, copies the new
  one and compiles it with `MetaEditor64.exe /compile /log`. The README gives the steps
  to load it, the inputs, phone alerts and troubleshooting.

**Not built:** a free-text AI chat window in MT5. The latest AI brief is shown as text
instead, and the AI still never trades. Twin-bar brackets and trend lines are also not
drawn.

**Checked here:** tests on hand-made bars for forming setups, A+ possible, both files,
the buttons, the indicator's source (never trades, writes only its command file) and that
the reference product's name appears nowhere but the owner's verbatim prompt. The chart
itself is checked by the owner on screen: no screenshot can be taken from this session.

### D115. S9: scalper modes side by side, HFT in five quick parts, HFT only with A/A+ setups

**The owner's request (2026-11-23):** let the bot trade at least two scalper modes, with
the choice of one, two or three; in HFT, open several trades at once ("5 of 0.3 lot"),
close them as soon as they move our way and repeat, only in favourable A+ setups; and
"the repetitive HFT scalping should stop as soon as the setup seems to stop or has
fulfilled the move". The demo balance is now 11,071.28 USD (read from MT5 before each
order: no code change).

**Risk, as asked and as built.** Five trades of 0.3 lots is 1.5 lots: at a typical
HFT stop of about 3.10 one loss would cost about 465 USD (4.2% of the account), and the
five positions would be stacked on one idea. That breaks the rules that are never
relaxed (small, controlled losses; no stacking). The owner chose instead (2026-11-23):
**0.25% risk for the whole HFT trade, split into five parts; targets at
0.25/0.5/0.75/1.0/1.25 R; HFT only in the direction of an A or A+ setup.** The five parts
are one trade with one stop and one risk budget, like the three children before; there
is still no martingale, grid, averaging down or hedging.

**Built:**

- **One, two or three modes.** `switch.scalper_modes` (config list, state key
  `switch.scalper_modes`); S5-S8's `switch.scalper_mode` is read only when no list is
  stored. `/scalper off | hft | hft,swing | hft swing | all`; the dashboard's Control
  tab has a multiselect; the chart panel's HFT, Intraday and Swing buttons are toggles
  that send the whole list (`PANEL_COMMANDS` holds `scalper:off` and every combination
  in the fixed order hft, intraday, swing). Each selected mode keeps its own books,
  magic number and one scalp at a time. `/status`, `/stats`, the snapshot and
  `scalp.json` show every selected mode; the Quick Scalp line is the newest decision of
  any selected mode.
- **The rules the modes share, per account** (`ScalperRunner.shared_refusal`; the
  account is the paper books together or the demo account):
  1. no scalp against another mode's open or waiting scalp (no hedging; the main
     strategy still comes first on the demo account, D92);
  2. a second scalp the same way only when every scalp open that way is risk-free (its
     stop at or past its entry); a waiting one blocks (no stacking);
  3. one set of daily limits for every mode: the scalper's daily loss in R
     (`daily_loss_limit_pct / risk_per_scalp_pct` = -4 R), its scalps a day (8, open
     ones included) and the 24-hour pause after 3 losses in a row, counted from the
     stored scalps of that account exactly as the risk manager counts them.
  Each mode's own executor still applies its own limits on top.
- **Up to five targets** (`TradePlan.take_profit_4/5`, `ExitReason.TP4/TP5`). The
  ladder rule is generalised: after TP1 the stop goes past the entry (breakeven+), after
  TP k (k >= 2) to TP k-1 (`previous_target`, by the highest target reached), applied by
  the paper book and `MT5Executor._manage` with the same functions. Three-part scalps
  behave as before (after TP2 to TP1).
- **Lot split.** A scalper split shares the lots out in whole volume steps, the largest
  remainders first (0.08 lots in five parts: 0.02/0.02/0.02/0.01/0.01, not
  0.01 x 4 + 0.04; ties go to the earlier part). This also changes three-part scalps
  slightly (0.08 lots: 3/3/2 steps instead of 3/2/3; 0.02 lots: TP1 and TP2 a step each
  instead of all at TP1). The main strategy's split is unchanged.
- **HFT** (`scalper.hft`): `split: [0.2 x 5]`, `step_r: 0.25`; the cost gate still
  needs TP1 at least twice the round-trip cost away and widens the step up to
  `tp_step_r_max` (2 R) when it is not (with this account's 0.30 round trip a 1.90 stop
  gets 0.60 steps). Each part is its own MT5 position with the stop and its own target
  (`GS-SC|<id>|1..5`). After a winning scalp the next HFT setup may enter at once (the
  cooldown is only after a stop-out): that is the repetition the owner asked for, one
  HFT trade at a time, within the shared limits. The 15-minute time stop and the step
  trail stay.
- **HFT only with an A or A+ setup, and only while it lasts**
  (`scalping/grade_filter.py`; `scalper.hft.grade_filter: a | a_plus | off`,
  `a_window_min: 60`). Each minute the bot gives the runner the main strategy's signals
  graded A or A+ in the last 60 minutes (`signals` table: grade, last target, the
  sweep's extreme, whether its trade has closed) and the setups forming now that can
  still be A+ ("A+ possible", S8). The runner judges each against the M1 bars since it
  formed (`ScalperRunner.path`). A setup stops allowing HFT its way when:
  - price reaches its last target (the move is fulfilled);
  - price runs back through the sweep's extreme (or its stop if none is stored): the
    setup failed;
  - its trade closes;
  - it is more than 60 minutes old;
  - for a forming setup, it is no longer forming (it became a signal, which then counts
    as above, or faded away).
  When one bar touches both the failure level and the target, failure is assumed (the
  worse case). An HFT candidate in any other direction is skipped and the reason shows
  in the Quick Scalp line, e.g. "HFT follows A or A+ setups: the A+ BUY 13:05 UTC setup
  reached its target". **An open HFT scalp is never closed by this:** it finishes under
  its own stop, targets and time stop, so the filter can only stop new risk, never add
  any. Intraday and swing scalping are unchanged.
- **Chart panel v3.1** (`mt5/GoldSweepPanel.mq5`, MetaEditor: 0 errors, 0 warnings):
  the mode toggles, TP4/TP5 lines on scalp boxes, TP4/TP5 hits, and the dashboard's
  trade column in up to six rows (targets two to a row). `scalp.json` carries
  `tp4`/`tp5` on scalp lines and `active_tp4/5` in the head; `mode` is the selection
  (`hft,swing`).

**Not done:** the A filter cannot be backtested with `mode_backtest`, which replays M1
bars without the main strategy's signals; the filter is judged on paper and on the demo
account (D111). The size the owner first asked for (5 x 0.3 lots) is not offered at any
setting.

**The HFT backtest with the five-part ladder** (`mode_backtest --mode hft`,
2024-01-01..2026-10-31, a 10,000 balance, this account's costs; without the A filter,
which the replay cannot apply):

| HFT ladder | Trades | A day | Won | PF | R a trade | Before costs | First 2/3 | Last 1/3 | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| Three parts, 0.5 R steps (D111) | 3,760 | 4.25 | 56.0% | 0.64 | -0.12 | -0.02 | | | 65.8% |
| Five parts, 0.25 R steps (S9) | 4,143 | 4.69 | 62.6% | 0.56 | -0.12 | -0.04 | -0.12 | -0.11 | 69.7% |

The quick ladder wins more often (62.6%) but takes less from each win, so it loses the
same 0.12 R a scalp after costs, with a lower profit factor and a deeper drawdown: by
exit, the full stop-outs (746, -1.01 R) outweigh everything the targets bring in. A
smaller first target does not fix HFT on its own. Whether trading only with a live A or
A+ setup does is the open question; it can only be answered on paper and the demo
account, which judge the mode (D111). Past results do not predict future ones.

**Checked here:** 1,030 tests (25 new: settings, parsing a selection, five parts in
whole steps, the ladder on paper and on the fake MT5 account, the HFT plan and its cost
gate, two modes side by side, the panel's list command, no hedging or stacking across
modes, the shared daily limits, every way an A/A+ setup stops allowing HFT, and the
filter end to end in the bot); the older HFT figures moved to the five-part ladder.

### D116. S10: the owner's capped recovery features for the scalper; every mode follows graded setups

**The owner's request (2026-11-25):** add martingale, grid, averaging down and hedging
"while keeping the risks reasonable", because A and A+ setups should lift HFT's win
rate towards 70%; let intraday and swing scalping auto-trade A+ to B setups; stop the
trades once the setup has done its move in our favour and wait for the next; in HFT,
stack trades only while price is moving our way; a lot cap (10 asked, 2 chosen).

**The rule this changes.** Rule 2 ("no martingale, grid, averaging down or hedging to
recover losses") was written as never relaxed. Before building, the owner was shown in
plain numbers why these features cannot make a losing edge win:

- doubling after each loss from 0.25% costs 7.75% of the account after five losses in
  a row;
- at a 70% win rate, HFT's small ladder wins (about 0.3 R) against 1 R losses still
  lose about 0.09 R a scalp.

The owner then chose **"Build all four, capped"**, after first answering "keep all" and
then clarifying it. **This is the owner's decision, recorded here.** Rule 2 now names this
one exception (CLAUDE.md):

- scalper only;
- paper and demo accounts only, never a real account;
- within the caps below.

The main strategy keeps rule 2 unchanged.

**The caps** (per account: all paper books together, or the demo account):

| Limit | Value |
|---|---|
| One scalp's risk, a martingale step included | at most 0.5% |
| A scalp plus its grid / averaging-down adds | at most 0.5% together |
| Open scalper risk | at most 0.75% of equity (on demo with the main strategy, through the account guard) |
| Daily scalper loss | 1%, now counted in money; a new scalp is sized down to what is left after the open risk, and skipped when nothing is left |
| Losses in a row | 3 pause every mode for 24 hours; a scalp and its adds count as one |
| Open positions per mode | at most 3 |
| Lots | at most 2 per magic number (`execution.max_total_lots: 2.0`, built-in 1.0) |
| Stop-loss and take-profit | on every order, adds included |
| Built-in defaults | everything off; `config.yaml` turns it on |

**Built** (`goldsweep/scalping/recovery.py`, pure functions shared by the runner and
`mode_backtest`):

- **Martingale** (`martingale_risk`).
  - After a losing scalp of a mode, that mode's next scalp risks the base × 2 (one step,
    so 0.25% becomes 0.5%).
  - It goes back to the base after a win or at a new trading day.
  - The results come from that account's stored scalps of the mode, a scalp and its adds
    counted as one.
- **Grid and averaging down, built as one mechanism** (`add_due`, `add_plan`).
  - While a scalp still has its first stop, a bar closing 0.4 R, then 0.7 R, against its
    entry adds a market position of 0.125% risk.
  - Each add has the same stop and one take-profit at the scalp's TP1. Its id is
    `<id>-A1` or `-A2`.
  - There are no adds once the stop has moved, when the setup it follows is no longer
    live, or past the 0.5% idea cap. A martingale-stepped scalp at 0.5% therefore gets
    none.
  - "Grid" and "averaging down" are the same thing here: evenly spaced entries inside
    one stop. A separate grid of orders on both sides was not built: it would be a hedge
    inside one mode.
- **HFT stacking** (`stack_refusal`).
  - Up to 3 HFT scalps at once.
  - A new one opens only while every open HFT position is risk-free (its stop at or past
    its entry, i.e. after TP1, the sign that price moved our way) and its setup is live.
- **Hedging between scalper modes** (`shared_refusal(hedge=True)`).
  - One mode may hold the side opposite another mode's scalp.
  - Never against the main strategy: `AccountGuard.check(hedge_exposures=...)` counts only
    the main strategy's side for its no-hedging rule, and a main order still closes
    opposite demo scalps first (D92).
  - Same-way scalps across modes still need the open one risk-free.
- **Sizing per plan:** `TradePlan.risk_pct` (at most 2%), used by `RiskManager.size` in
  the paper book and `MT5Executor.submit`, and saved with the plan. Placed-scalp messages
  show the risk and why (martingale step, sized to the daily budget, stacked, add n of 2).
- **Every mode follows graded setups.**
  - HFT keeps A or A+ within 60 minutes.
  - Intraday follows A+, A or B within 4 hours; swing scalping A+, A or B within 8 hours
    (`grade_filter: b`, `setup_window_min`).
  - `Trader._mode_setups` reads them for each selected mode.
- **Stop when the setup is done.**
  - The runner remembers the setup each scalp follows (`scalper.follows`, kept over a
    restart).
  - When that setup reaches its last target, the scalp and its adds are closed at market
    with exit `setup_done` (`close_plan` on the paper book and the MT5 executor). The
    mode then waits for the next setup.
  - A failed setup closes nothing: the scalp's own stop handles it.
- **Not built:** the 10-lot cap (the owner chose 2), and recovery features on a real
  account or in the main strategy.
- **A flaw found by the backtest and fixed before reporting.** A bar can close far past
  an add's level, right next to the stop. The first version then sized the add on that
  tiny distance (2.99 lots for 0.125%), and a gap through the stop lost 0.95% at once.
  Now an add is never larger than it would be at its own level: its risk shrinks in
  proportion when the close is nearer the stop (`add_plan(level_r=...)`).

**The backtests** (`mode_backtest`, 2024-01-01..2026-10-31, a 10,000 balance, this
account's costs; trades include adds; the grade filters, the setup-done close and
hedging between modes cannot be replayed):

| Mode | Recovery | Trades | Won | PF | R a trade | Return | Max DD | Worst trading day |
|---|---|---|---|---|---|---|---|---|
| HFT | on | 5,455 | 57.2% | 0.55 | -0.16 | -86.1% | 86.1% | -1.04% |
| HFT | off | 4,153 | 62.6% | 0.56 | -0.12 | -69.6% | 69.6% | |
| Intraday | on | 2,616 | 37.8% | 0.91 | -0.06 | -23.2% | 31.6% | -1.11% |
| Intraday | off | 1,381 | 50.3% | 0.93 | -0.03 | -10.8% | 18.3% | |
| Swing scalping | on | 1,437 | 35.5% | 0.81 | -0.12 | -22.8% | 24.4% | -0.99% |
| Swing scalping | off | 817 | 46.8% | 0.85 | -0.07 | -12.0% | 13.6% | |

**What this shows.**

- In every mode the recovery features roughly double the loss and deepen the drawdown.
  Adds fill on the way to the stop and mostly stop out with the scalp; martingale steps
  come after losses, when the next scalp is no likelier to win.
- The caps work: the worst trading day stays near the 1% limit. The overshoot, up to
  0.11%, is fills beyond the stop.
- The win rate does not rise towards 70%. Without the A filter, HFT wins 57-63%, and its
  wins are too small for that to pay.
- Whether the A/A+ filter changes this is the open question. It can only be judged on
  paper and the demo account (D111), with the recovery features switched on or off in
  `config.yaml`.

Past results do not predict future ones.

**Checked here:**

- 1,049 tests. The new `tests/test_s10_recovery.py` covers:
  - settings and defaults;
  - martingale steps and resets;
  - add levels, sizing and caps;
  - stacking rules;
  - open risk and the daily budget;
  - per-plan sizing on paper and the fake MT5;
  - `close_plan`;
  - the guard's hedge rule and hedging between modes;
  - a scalp and its adds counted as one;
  - no recovery on a real account;
  - the martingale step, grid adds, stacking and the setup-done close in the bot;
  - B-grade setups for intraday and swing;
  - a sliver of daily budget.
- The older pins moved: messages now show each scalp's risk, the per-mode book limits
  rose, and the daily limit is in money.

**The owner's approval (2026-11-25): S10 approved with the recovery features OFF.** After
the backtest above, the owner chose to keep martingale, grid / averaging down, HFT
stacking and hedging between modes switched off (`enabled: false`, `hedge_modes: false`
in `config.yaml`); the code, caps and tests stay, so any one can be switched on later.
What runs: intraday and swing follow A+, A or B setups, HFT follows A or A+, every scalp
closes when its setup reaches its last target, the daily limit counts money, and the
lot cap is 2. Tests of the features use `tests.helpers.with_recovery`.

### D117. All scalper modes on the demo account; HFT and intraday without the grade filter

**The owner's request (2026-11-25):** approve every scalper mode on the demo account, and
switch the grade filter off for HFT and intraday "for now".

**Done:**

- `scalper.approved` is now `[hft, intraday, swing]`, as three `/approve scalper <mode>`
  commands would set it. Selected modes send their scalps to the demo account (this is a
  demo account, so D93's forward-test file is not needed). `/revoke scalper <mode>` returns
  one to paper.
- `config.yaml`: `scalper.hft.grade_filter: "off"` and
  `scalper.intraday.grade_filter: "off"`. HFT and intraday now trade on their own rules
  alone, as in their backtests (D111, D112), without waiting for a main-strategy setup.
  Swing scalping still follows A+, A or B setups.
- With no setup followed, a scalp is never closed by the setup-done rule: it runs to its
  own stop, targets and time or flat-by exit.
- The built-in defaults keep the filters on (HFT `a`, intraday `b`); tests of the filters
  use `tests.helpers.with_grade_filters`.

**Risk, stated:**

- In the backtests without the filter, HFT lost 0.12 R a scalp after costs, with a 70%
  drawdown over 2024-2026. Intraday lost 0.03 R, with an 18% drawdown.
- On the demo account the caps still hold every scalp to 0.25% risk, the scalper to a 1%
  daily loss and 8 scalps a day, with the 3-losses pause and 2 lots a magic number.
- The recovery features stay off (D116).

### D118. S11: signals only, retest entries, graded scalp signals, reminders

**The owner's request (2026-11-25):** forget the "flip $20 to $200" idea and auto-trading.
GoldSweep becomes a signal system: A+, A and B setups of the main strategy and of the
HFT, intraday and swing scalps, analysed with trends, levels and news, sent to Telegram
with SL and TP1/TP2/TP3 (TP4/TP5 for HFT). The owner places every trade by hand.

Added while it was built:

- every entry should wait for the market's **retest** of the price it just left ("the
  perfect entry");
- a reminder to **move the stop to breakeven after TP1**;
- every signal should say **which kind it is** (main, HFT, intraday, swing).

The owner chose two messages for main setups (the plan, then "price is at the entry
zone"), one for scalps (when the retest is confirmed), and every grade sent (A+, A, B).

**Built:**

- **Signals only** (`execution.signals_only: true` in `config.yaml`; built-in false):
  - the main strategy sends every graded setup (A+, A and B) as a manual trade, whatever
    `auto_trade` says, and places nothing;
  - the scalper's modes stay on paper even when approved for the demo account (the
    approvals are kept and do nothing);
  - nothing at all is sent to MT5;
  - the start message and `/status` open with "📲 SIGNALS ONLY: you place every trade;
    nothing is sent to MT5";
  - the paper books still track every scalp signal, so `/stats` shows how the signals
    would have done, and the scalper's daily limits (8 a day, a pause after 3 losses)
    cap the signals.
- **Main signals:**
  - **message 1:** "📲 MAIN SIGNAL A+ ..." with a `Type:` line ("MAIN TRADE: M5
    liquidity sweep, held for hours"), entry, SL, TP1-TP3, the suggested lots for the
    account at the risk setting, and "MANUAL TRADE: place a limit at 2028.90 (FVG mid:
    the retest zone), valid until 09:55 UTC. After TP1, move your stop to breakeven."
  - **The retest for the main strategy already existed.** It waits up to 2 hours for
    price to come back into the gap the move left, its best-tested entry (D82: +0.52 R a
    trade in 2024-2026). A tighter M1 confirmation made it worse (D83) and stays off.
  - **The watcher** (`execution/signal_watch.py`, a pure `advance(watch, bars)`; state
    `signals.watch`, kept over a restart; run each minute after the scalper's bars)
    follows each manual setup on M1 bars and sends:
    - 🎯 "price is at the entry zone now";
    - ✅ "TP1 reached: take part profit and move your stop to breakeven";
    - 🔴 "the stop was hit";
    - ⌛ "no retest before ...: do not enter";
    - ❌ "missed: do not chase" (TP1 came first);
    - ❌ "setup failed" (the stop came first).

    The same worse-case rules as the simulator apply.
- **Scalp retest entries** (`scalping/retest.py`, shared by the runner and
  `mode_backtest`; `scalper.retest`).
  - **The level.** A scalp no longer enters on its signal bar; it names its retest level:
    - sweep and reclaim: the swept level;
    - range breakouts: the range edge;
    - VWAP trend: the VWAP;
    - EMA pullback: the EMA;
    - any other setup: 35% of the way back to its stop anchor.
  - **The watch.** Each M1 bar is checked:
    - **confirmed:** the bar comes back to the level (within 0.1 ATR) and closes back
      the trade's way. The entry is that close.
    - **missed:** TP1 comes first, or the window ends (HFT 10, intraday 30, swing 60
      minutes).
    - **failed:** the stop anchor is crossed first.
  - **The stop.** A retest entry sits close to the anchor, so a stop nearer than the
    minimum (0.5 ATR) is widened to that minimum, still beyond the anchor; the size
    shrinks with it (`Candidate.retested`). Market entries keep the old rule.
- **Scalp grades** (`scalping/grading.py`, from the main strategy's picture each minute:
  H1 bias, unswept pools, the next release, the live graded main setups via
  `backing_grade`):
  - **A+:** a live A+ main setup (or "A+ possible") its way, with the H1 bias, no
    high-impact release within 30 minutes;
  - **A:** a live A or A+ main setup, or a key level (session or twin-bar level, or a
    pool within 0.2 ATR) with the H1 bias, and no release within 30 minutes;
  - **B:** with the H1 bias, or the bias is neutral;
  - **none:** against the H1 bias with no main setup: recorded, not sent
    (`signal_min_grade: B`).

  The pressure meter is shown, never scored.
- **The scalp signal** ("📲 SCALP SIGNAL HFT A BUY XAUUSD (manual trade)") contains:
  - its `Type:` line (HFT: "1-minute chart, held seconds to 15 minutes"; intraday: "closed
    by the New York close"; swing: "held hours to two days");
  - "Enter now ~2029.10: retest of 2028.50 confirmed";
  - the SL and every TP;
  - the suggested lots for the account;
  - why the grade;
  - the setup, the market (H1 bias, news bias, pressure as an estimate, the next
    release) and the nearest levels;
  - "Valid 2 min: skip it if price is already past ...";
  - "After TP1, move your stop to breakeven".

  When the tracked scalp reaches TP1, "✅ ... TP1 reached ... move your stop to
  breakeven (2029.20, just past the entry to cover costs)" follows. When it ends, "📊
  Signal result (tracked on paper)" follows.
- **Not built:** auto-starting the bot or a VPS. Signals need the PC on, awake and online
  with MT5 and the bot window open during the hours wanted (London and New York, about
  10:00-00:00 Kampala time). `deploy\install-tasks.ps1` can start it at set times.

**The retest backtest** (`mode_backtest`, 2024-01-01..2026-10-31, a 10,000 balance, this
account's costs, the grade filters off, recovery off; "market" enters on the signal bar
as before):

| Mode | Entry | Trades | Won | PF | R a trade | Return | Max DD | Retests: confirmed / missed / failed |
|---|---|---|---|---|---|---|---|---|
| HFT | market | 4,153 | 62.6% | 0.56 | -0.12 | -69.6% | 69.6% | |
| HFT | retest | 873 | 48.7% | 0.46 | -0.24 | -43.9% | 44.3% | 1,081 / 5,027 / 912 of 7,411 |
| Intraday | market | 1,381 | 50.3% | 0.93 | -0.03 | -10.8% | 18.3% | |
| Intraday | retest | 822 | 45.7% | 0.75 | -0.13 | -23.3% | 26.3% | 1,014 / 920 / 172 of 2,396 |
| Swing | market | 817 | 46.8% | 0.85 | -0.07 | -12.0% | 13.6% | |
| Swing | retest | 623 | 46.2% | 0.88 | -0.07 | -8.0% | 10.0% | 888 / 379 / 44 of 1,399 |

**What this shows.**

- **HFT and intraday:** waiting for the retest made each signal worse (-0.24 R against
  -0.12 R, and -0.13 R against -0.03 R). The setups that do come back to their level are
  more often the ones that fail.
- **HFT's total loss is smaller** only because two in three setups never retest, so far
  fewer trades are taken.
- **Swing scalping:** the result per trade is unchanged, with fewer trades and a
  shallower drawdown.
- **Worst trading days** stay near the 1% daily limit (HFT -1.13%, intraday -0.69%,
  swing -1.34%: fills beyond the stop and positions held across the day boundary).
- **The main strategy's** retest (the gap middle) remains its best-tested entry (D82).
- **Overall:** for the scalps this history does not support the retest as "the perfect
  entry". It is shipped on, as the owner asked, and each mode can drop it from
  `scalper.retest.modes`.

Past results do not predict future ones.

**Checked here:** the full test suite, including 18 new tests in
`tests/test_s11_signals.py`:

- the shipped settings;
- the retest level, confirmation, miss and failure, the entry moving to the retest, and
  the widened minimum stop;
- grading and the main setup behind a scalp;
- the scalp and main signal messages;
- the main-setup watcher (entry, TP1, stop, expiry, miss; kept over a restart) and its
  messages;
- the bot following a manual main setup;
- a scalp signal after its retest with the breakeven reminder;
- no signal when the retest never comes;
- signals only sending nothing to MT5, with the approvals kept;
- a main setup sent as a manual trade;
- the status line;
- the backtest's retest.

The older tests run with signals only and the retest switched off in the shared fixture.

**The owner's approval (2026-11-25): S11 approved, with the retest for swing scalping and
the main strategy only** (`scalper.retest.modes: [swing]`; the main strategy keeps its
gap-middle entry and the watcher's "price is at the entry zone" alert). HFT and intraday
scalps enter on their signal bar, as in their better backtest, and their Telegram signals
carry no retest wording ("Enter now ~2030.00").
