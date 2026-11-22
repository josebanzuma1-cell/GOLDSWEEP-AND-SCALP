"""Setups forming before they trigger, and whether they can still be A+ (S8, D114).

On the hand-made London raid of tests/test_signals.py: the Asian low (2027.00) is
pierced at 07:35 and closed back inside on that bar, the shift comes at 07:45 and the
signal at 07:55. Before the signal the raid is a FORMING mark on the chart.
"""

from __future__ import annotations

import dataclasses

import pandas as pd

from goldsweep.analysis.grades import a_plus_possible
from goldsweep.analysis.signals import FORMING_STAGES, ny_midnight_open
from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import PlaybookRule, Settings
from tests.helpers import bars, utc
from tests.test_signals import MONDAY, RAID, SPREAD, engine_for, feed, with_raid


def forming_after(settings: Settings, rows: list) -> tuple[list, pd.DataFrame]:
    engine = engine_for(settings)
    run = feed(engine, rows)
    frame = bars(rows, MONDAY, spread=SPREAD)
    return engine.forming(frame), run.signals


def test_a_swept_pool_is_forming_until_its_signal(settings: Settings) -> None:
    found, signals = forming_after(settings, with_raid(RAID[:8]))  # 07:35 swept, no shift yet
    assert signals == []
    (forming,) = [f for f in found if f.pool.price == 2027.0]
    assert (forming.direction, forming.stage, forming.since) == (
        Direction.LONG,
        "swept",
        utc(2026, 11, 17, 7, 35),
    )
    assert forming.extreme == 2026.4 and forming.model == "sweep_mss"
    # New York midnight is 05:00 UTC: that bar opened at 2030.00, above the raid's low.
    assert forming.judas
    assert forming.describe == FORMING_STAGES["swept"]
    found, signals = forming_after(settings, with_raid())  # the signal formed
    assert len(signals) == 1
    assert not [f for f in found if f.pool.price == 2027.0]


def test_a_pierced_pool_waits_for_the_close_back_inside(settings: Settings) -> None:
    rows = with_raid([*RAID[:7], (2027.7, 2027.8, 2026.4, 2026.6)])  # closes below 2027.00
    found, _ = forming_after(settings, rows)
    (forming,) = [f for f in found if f.pool.price == 2027.0]
    assert forming.stage == "pierced"


def test_ny_midnight_open() -> None:
    frame = bars([(2030.0, 2030.5, 2029.5, 2030.2)] * 3, utc(2026, 11, 17, 4, 55))
    assert ny_midnight_open(frame, utc(2026, 11, 17, 8)) == 2030.0  # the 05:00 UTC bar
    assert ny_midnight_open(frame.iloc[:1], utc(2026, 11, 17, 8)) is None


def test_a_plus_possible(settings: Settings) -> None:
    found, _ = forming_after(settings, with_raid(RAID[:8]))
    (forming,) = [f for f in found if f.pool.price == 2027.0]
    playbooks = settings.config.playbooks
    # London's A+ candidate: sweep + MSS with the Judas tag (D86).
    assert a_plus_possible(forming, playbooks, "london")
    assert a_plus_possible(forming, playbooks, "overlap")  # New York's playbook
    assert not a_plus_possible(forming, playbooks, "asia")  # no candidate there
    assert not a_plus_possible(forming, playbooks, None)
    assert not a_plus_possible(dataclasses.replace(forming, judas=False), playbooks, "london")
    assert not a_plus_possible(dataclasses.replace(forming, model="cisd"), playbooks, "london")
    # A candidate that also needs a tag the raid cannot show yet: no promise.
    smt = PlaybookRule(model="sweep_mss", min_score=60, tags_any=("judas",), tags_all=("smt",))
    london = playbooks.london.model_copy(update={"candidate": smt})
    assert not a_plus_possible(forming, playbooks.model_copy(update={"london": london}), "london")
