"""GoldSweep entry point.

Phase 1 implements the environment check:

    python main.py --check            MetaTrader 5 (use a DEMO account)
    python main.py --check --mock     offline, synthetic prices

The scheduler and trading loop are added in later phases. GoldSweep runs on
the Windows PC or VPS that hosts the MetaTrader 5 terminal, not on a phone.
"""

from __future__ import annotations

import sys

from goldsweep.cli import main

if __name__ == "__main__":
    sys.exit(main())
