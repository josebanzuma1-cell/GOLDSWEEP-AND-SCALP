# Scalper modes: research and plan (fast "HFT", intraday, swing)

The owner asked (2026-11-22) for research on scalper bots that open many trades and
close them within seconds once price moves their way ("HFT mode"), on intraday and
swing modes and any other modes that make a good scalper; for a plan; for a switch
between them; and for each mode to keep the tools that help it and drop the ones
that do not. This follows S1 (D101): the first scalper lost after costs on 2024-2026.

Nothing here is built yet. Decisions are D102-D107 in `DECISIONS.md`.

## 1. What the research says

**Retail "HFT" gold robots are tick scalpers, not HFT.** True high-frequency trading
runs on exchange order books from co-located servers in microseconds. The gold
"HFT" expert advisors sold for MT5 trade XAUUSD on ticks or M1 and close within
seconds to minutes. Their sellers all say results swing on spread, commission,
slippage, broker and latency, recommend raw-spread accounts and a VPS a few
milliseconds from the broker, and publish no independent results. One author who
tested tick-level gold edges found most were smaller than the spread, and the one
that survived needed about 50 ms execution or faster. [1][2][3][4]

**Latency arbitrage is forbidden.** Exness reportedly can cancel profits from trading
on price latency, arbitrage or "automated system overload"; prop firms and other
brokers ban the same. GoldSweep must never trade on a slow feed or flood the server. [5][6]

**What drives seconds-scale moves.** On exchanges, short-term price changes are mostly
explained by order-flow imbalance at the best bid and ask [7]. Our broker gives no
order book for XAUUSDm (measured below), so a tick bot here can only use price,
tick rate and spread: weaker information than the professionals have.

**News moves gold within seconds, but not tradeably here.** Gold futures absorb most
of a US macro surprise within about 90 seconds (payrolls, CPI, GDP, unemployment)
[8][9]. That is the one fast pattern with academic support, but spreads widen and
fills slip at those moments, the spec's news blackout forbids new trades 30 minutes
before and 15 after a release, and broker terms scrutinise news-spike trading.
It stays out.

**Intraday.** The opening-range breakout paper reproduced gross on index CFDs but came
to about zero after spread and slippage in an independent replication [10][11];
the VWAP trend strategy is the authors' backtest without spread [12]; intraday
momentum in gold is mixed and depends on market hours [13]. For gold's London
breakout of the Asian range, only vendor and blog tests exist, and they warn that
fixed-spread backtests flatter it [14]. The sweep + MSS model GoldSweep already
trades is itself an intraday mode with the best evidence we have (D84: +0.93 R).

**Swing.** Time-series momentum (trend following over weeks to a year) is the best
documented of all: 58 futures over decades, commodities included, gold in the
universe; weaker after 2009, especially in commodities [15][16].

**Mean reversion** (RSI(2), Bollinger fades): no rigorous test on intraday gold; the
original evidence is on US stocks without costs, and practitioners call trending
commodities a poor fit [17].

## 2. What we measured (2026-11-21/22, read-only)

| Fact | Value | Meaning |
|---|---|---|
| Ping to Exness-MT5Trial10 from this laptop | 69 ms | the fast mode needs a Windows VPS near the server (< 10-20 ms); the MQL5 VPS cannot run Python |
| XAUUSDm spread | 0.24 USD median (0.16-0.28), all hours | the cost of every round trip |
| Commission on our demo deals | 0.00 (122 deals) | the backtests charge 7.00 a lot: too high for this account (D107) |
| Order book (market depth) for XAUUSDm | not available | no order-flow strategies |
| Ticks, Friday 13:00-15:00 UTC | 10.7 a second | enough for a tick loop |
| Median price move over 5 / 10 / 30 / 60 s | 0.49 / 0.67 / 1.11 / 1.57 USD | the spread is 49% / 36% / 22% / 15% of it |
| Median move of a one-minute / five-minute bar | 1.95 / 4.53 USD | costs are 16% / 7% of it |
| Tick history the broker serves | about two weeks | a fast mode can only be tested on ticks we record ourselves |
| Daily / H4 history | from 2014-01-14 (3,958 days) | enough to test a swing mode |
| Overnight swap | longs -43 USD a lot a night (triple on Wednesday), shorts 0 | swing longs must pay it in tests |

**What this means for a seconds-long mode:** to close "in seconds after a favourable
move", the move must first pay the 0.24 spread. A typical 10-second move is 0.67, so a
third of it is gone before the trade starts; the S1 scalper, with one-minute stops,
already lost 0.1-0.2 R a trade to costs. A fast mode can only work if its entries pick
moves much larger than typical, and that can only be shown on recorded ticks and a
fast VPS. The plan builds the measurement first and lets the result decide.

## 3. The plan: one scalper, three modes, a switch

**Modes** (each a strategy of its own, with its own tools):

| Mode | Holds | Trades | Entries to test | Exits |
|---|---|---|---|---|
| **Tick** (the "HFT" request) | seconds to 2 min | many a session, one at a time | velocity burst (price moves k x its usual 5 s move with the tick rate up); micro-sweep fade (a 60 s high/low pierced and reclaimed within seconds); session-open burst (London, COMEX open) | TP at least 2x the spread; profit lock: after +x the stop goes past the entry and trails tick by tick; time stop 60-120 s; hard SL and TP at the broker on every order |
| **Intraday** | minutes to hours, flat by the New York close | 0-3 a day | Asian-range breakout at the London open; New York opening range; session-VWAP trend; the S1 sweep reclaim on M5 | ladder TP1/TP2/TP3, breakeven at TP1, step trail; flat before rollover |
| **Swing** | days to weeks | a few a month | daily trend: Donchian 20/55 breakout, 1-3-12 month momentum, pullback to the 50-day average in a 200-day trend | ATR stop, trailing on the daily channel; swap paid every night |

**Tools kept and dropped per mode:**

| Tool | Tick | Intraday | Swing |
|---|---|---|---|
| Tick stream, tick rate, price velocity (new) | keep | drop | drop |
| Live spread monitor and spread-to-move gate | keep | keep | drop (spread tiny on a daily stop) |
| Latency monitor (ping, order round trip) (new) | keep (stops the mode above a limit) | keep | drop |
| Session clock, kill zones, rollover, Friday cutoff | keep | keep | entries only |
| News blackout | keep | keep | keep (no new entries) |
| Opening range, Asian range, session VWAP (new) | drop | keep | drop |
| Step trail (S1) | keep (as a tick-scale trail) | keep | drop (daily channel instead) |
| Micro swings, twin bars, sweeps (S1) | micro-sweep only | keep | drop |
| H1 bias filter | drop (did not help in S1) | test on/off | drop |
| ATR | per-second ATR | M1/M5 | D1 |
| Donchian channel, 50/200-day averages, momentum (new) | drop | drop | keep |
| Swap cost model (new) | drop | drop | keep |
| FVG, order blocks, ICT tags | drop (main strategy keeps them) | drop | drop |
| LLM | never (too slow, never on the order path) | never | never |

**The switch.** `config.yaml` sets the defaults; Telegram and the dashboard change them
while running:

- `/mode main|scalper|both` - which strategies may open trades (the main sweep + MSS
  system, the scalper, or both side by side; the kill switch stops all).
- `/scalper off|tick|intraday|swing` - the scalper's mode, one at a time.
- `/status` shows both, with each mode's state: **off**, **paper** (monitored on a
  shadow book, as playbook candidates are) or **trading**.
- A mode can be switched to **trading** only after its evidence gate passed and the
  owner sent `/approve scalper <mode>`; otherwise switching to it runs it on paper.

**Not built, whatever the switch says:** latency arbitrage; news-spike trading; grid,
martingale, averaging down or hedging to recover; stacking positions (one scalp at a
time, its children within one risk budget); order-flow strategies (no order book).

## 4. Evidence gates (as for every strategy here)

A mode may trade on the demo account only when, on data it was not tuned on:

1. its backtest is positive **after realistic costs** (measured spread, no commission
   on this account, slippage, the broker delay; swap for swing), with enough trades
   (tick 300+, intraday 100+, swing 30+) and a positive held-out last third;
2. 20 paper trades on the live feed (shadow book) have a positive average;
3. the owner approves it.

A mode that fails stays off, like S1, and its report stays.

## 5. Phases (each stops for approval)

- **S2 - Switch, tick recorder, costs.** The `/mode` and `/scalper` switches with
  off/paper/trading states; a tick recorder that saves every XAUUSDm tick from now on
  (daily compressed files in `data/ticks/`); the cost model per account (commission
  0 here, D107); latency logging (ping, order round trip).
- **S3 - Intraday mode.** Asian-range breakout, New York opening range, VWAP trend,
  sweep reclaim on M5; backtest on the stored M1 history 2024-2026; verdict.
- **S4 - Swing mode.** Donchian, momentum and pullback rules on D1/H4 2014-2026 with
  swap; walk-forward; verdict.
- **S5 - Tick mode.** The three entries and the fast exits on 1-second bars built from
  recorded ticks, with the measured delay; verdict once 8 weeks of ticks are recorded
  (the recorder runs from S2), and only with a VPS ping under 20 ms.
- **S6 - Running alongside.** Modes that passed run on paper, then (after `/approve`)
  on the demo account: the tick mode in its own process (D105), the others in the bot;
  the account guard, no opposite positions, the kill switch and `/golive` cover all.
- **S7 - Chart, assistant, Telegram** (the original S3-S5) for the modes that run.

## Sources

1. [XAU Tick Scalper EA MT5](https://www.mql5.com/en/market/product/180216) (seller page)
2. [Gold Catalyst Evolution: VPS, spreads, brokers and execution reality](https://www.mql5.com/en/blogs/post/772598)
3. [Choosing a broker for a gold EA: spread, stop level, commission and slippage](https://www.mql5.com/en/blogs/post/776956)
4. [MT5 VPS for gold EAs: slippage and latency](https://fxnx.com/en/blog/mt5-vps-gold-eas-stop-slippage-latency)
5. [Does Exness allow bot trading? (review of the client agreement)](https://brokerlistings.com/reviews/exness/does-exness-allow-bot-trading)
6. [Latency arbitrage prohibition (prop firm example)](https://intercom.help/onlyfundsfx/en/articles/11117308-latency-arbitrage-prohibition)
7. [Cont, Kukanov, Stoikov: The price impact of order book events](https://arxiv.org/abs/1011.6402)
8. [Smales and Yang: belief dispersion and gold futures' response to announcements](https://espace.curtin.edu.au/handle/20.500.11937/40677?show=full)
9. [Macroeconomic announcements and gold futures in a VAR-GARCH framework](https://espace.curtin.edu.au/handle/20.500.11937/28039?show=full)
10. [The opening-range breakout paper replicated on five indices: gross reproduced, net zero](https://www.mql5.com/en/blogs/post/776235)
11. [Zarattini, Barbon, Aziz: ORB for stocks in play](https://ideas.repec.org/p/chf/rpseri/rp2498.html)
12. [Zarattini, Aziz: VWAP, the holy grail for day trading systems](https://concretumgroup.com/volume-weighted-average-price-vwap-the-holy-grail-for-day-trading-systems/)
13. [Intraday momentum in metal futures (gold, silver)](https://acfr.aut.ac.nz/__data/assets/pdf_file/0008/686816/4a-Z-Ivy-Zhou.pdf)
14. [London breakout on gold: 3.7 years of backtests](https://www.mql5.com/en/blogs/post/777540)
15. [Moskowitz, Ooi, Pedersen: Time series momentum](https://papers.ssrn.com/abstract=2089463)
16. [Time-series momentum across equity and commodity markets](https://pure.manchester.ac.uk/ws/portalfiles/portal/39718724/The_Trend_is_Your_Friend_Time_Series_Momentum_Strategies_Across_Equity_and_Commodity_Markets.pdf)
17. [Day trading Larry Connors RSI2 mean-reversion strategies](https://www.mql5.com/en/articles/17636)

Seller pages, broker blogs and single-author posts are claims, not evidence; they are
listed because they describe how these robots work, not because their results hold.
