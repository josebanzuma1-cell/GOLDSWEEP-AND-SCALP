"""The analysis engine: structure, liquidity, zones, Candle Range Theory and signals.

Everything here is a pure function of candle frames (see
:mod:`goldsweep.data.candles`), so the same code runs live and in backtests.
Only closed bars are ever passed in, which is what keeps the engine free of
look-ahead.
"""
