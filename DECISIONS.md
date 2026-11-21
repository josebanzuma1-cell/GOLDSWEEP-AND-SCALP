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
- **Confirmation.** After each finished monitored trade the owner gets its result
  and the tally (finished, won, average R). When `playbooks.monitor_trades` (20)
  have finished with a positive average, one message asks for
  `/approve <session>` (asked once per candidate fingerprint).
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
