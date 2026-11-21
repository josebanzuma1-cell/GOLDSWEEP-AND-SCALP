"""``python -m goldsweep.playbook --from 2024-01-01 --to 2026-10-31`` (Phase 10, D86).

Finds each session's A+ candidate on the stored history (see
:mod:`goldsweep.playbook.analysis`) and writes ``reports/playbook/``. It changes
nothing: the suggested candidates are copied into config.yaml by hand.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from goldsweep.analysis.grades import PLAYBOOK_SESSIONS, describe_rule
from goldsweep.analysis.signals import MODELS
from goldsweep.backtest.cli import HistoryError, stored_data
from goldsweep.backtest.engine import build_simulator, tag_context, tag_smt
from goldsweep.core.logging import setup_logging, shutdown_logging
from goldsweep.core.settings import ConfigError, Settings, load_settings
from goldsweep.data.broker import BrokerError
from goldsweep.data.factory import create_broker
from goldsweep.playbook.analysis import (
    DISCLAIMER,
    candidate_rules,
    choose,
    evaluate,
    signals_for,
    suggested_yaml,
    write_report,
)
from goldsweep.storage.db import Database


def analysis_settings(settings: Settings) -> Settings:
    """Every model on, and setups allowed in the Asian session, for the analysis only."""
    config = settings.config
    sessions = config.sessions.model_copy(
        update={"asia": config.sessions.asia.model_copy(update={"trade": True})}
    )
    models = config.models.model_copy(
        update={
            name: getattr(config.models, name).model_copy(update={"enabled": True})
            for name in MODELS
        }
    )
    return dataclasses.replace(
        settings, config=config.model_copy(update={"sessions": sessions, "models": models})
    )


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m goldsweep.playbook",
        description="Find each session's A+ playbook candidate on the stored history.",
    )
    parser.add_argument("--from", dest="start", type=_day, required=True)
    parser.add_argument("--to", dest="end", type=_day, required=True)
    parser.add_argument("--offline", action="store_true", help="never contact MT5")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--jobs", type=int, default=len(MODELS), help="parallel signal passes")
    args = parser.parse_args(argv)
    try:
        settings = analysis_settings(load_settings(config_path=args.config))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    start = dt.datetime.combine(args.start, dt.time(0), tzinfo=dt.UTC)
    end = dt.datetime.combine(args.end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
    split = start + (end - start) * 2 / 3
    config = settings.config
    load_start = start - dt.timedelta(days=config.backtest.warmup_days)

    def say(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    setup_logging(settings.log_dir, console=False)
    try:
        with Database(settings.db_path) as db:
            data = stored_data(
                settings,
                db,
                load_start,
                end + dt.timedelta(days=5),
                download=False,
                offline=args.offline,
                with_m1=False,
                broker_factory=lambda s: create_broker(s),
                say=say,
            )
        cache = settings.data_dir / "backtest-cache"
        say(f"Signal passes for {', '.join(MODELS)} (cached after the first run)...")
        jobs = [(settings, data, start, end, cache, model) for model in MODELS]
        with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            passes = list(pool.map(signals_for, jobs))
        records = sorted((r for found in passes for r in found), key=lambda r: r.time)
        records = tag_context(settings, data, tag_smt(settings, data, records))
        simulator = build_simulator(settings, data)
        rules = candidate_rules(list(MODELS))
        say(f"Trading {len(rules)} rules in {len(PLAYBOOK_SESSIONS)} sessions...")
        results = evaluate(
            simulator,
            records,
            rules,
            start=start,
            split=split,
            end=end,
            neutral_extra=config.strategy.bias.neutral_extra_score,
            balance=config.backtest.initial_balance,
        )
    except (HistoryError, BrokerError) as exc:
        print(f"No usable price history: {exc}", file=sys.stderr)
        return 1
    finally:
        shutdown_logging()
    choices = choose(results)
    folder = args.out or settings.config_path.parent / "reports" / "playbook"
    period = f"{args.start:%Y-%m-%d} .. {args.end:%Y-%m-%d}"
    report = write_report(results, choices, folder, period=period, split=split)
    for choice in choices:
        if choice.result is None:
            print(f"{choice.session:<8} no candidate: {choice.reason}")
            continue
        r = choice.result
        print(
            f"{choice.session:<8} {describe_rule(r.rule)} | choosing {r.choosing.trades} trades, "
            f"{r.choosing.expectancy_r:+.2f} R | held out {r.held_out.trades} trades, "
            f"{r.held_out.expectancy_r:+.2f} R"
        )
    print("\nSuggested playbooks (config.yaml):\n" + suggested_yaml(choices))
    print(f"\nReport: {report}\n{DISCLAIMER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
