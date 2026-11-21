"""The M1 scalper add-on (docs/SCALPER_SPEC.md, docs/SCALPER_PLAN.md, D88-D99).

It runs alongside the main sweep + MSS strategy, never instead of it. Everything
here works on closed bars only, one bar at a time, so the backtest and the live
bot see the same thing:

* :mod:`~goldsweep.scalping.trail`: the step-trail line (trend and trailing stop)
  and a rolling ATR;
* :mod:`~goldsweep.scalping.levels`: micro liquidity (recent swing points, twin
  bars, session ranges) and the sweeps of it;
* :mod:`~goldsweep.scalping.engine`: the BUY/SELL setups;
* :mod:`~goldsweep.scalping.plan`: stop-loss, the TP ladder and the cost gate;
* :mod:`~goldsweep.scalping.backtest`: the M1 backtest.

Trades are managed by the shared :class:`~goldsweep.execution.book.TradeBook` with
the scalper's :class:`~goldsweep.execution.book.Management` rules.
"""
