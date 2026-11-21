"""Clock and session calculator, including the daylight-saving cases the spec asks for.

Daylight-saving dates used below:

    year   US starts   UK starts   UK ends     US ends
    2025   9 Mar       30 Mar      26 Oct      2 Nov
    2026   8 Mar       29 Mar      25 Oct      1 Nov
    2027   14 Mar      28 Mar      31 Oct      7 Nov

Between the US and UK dates the two countries are out of step, which moves the
New York windows (in March) or the London windows (in October) by one hour
relative to each other.
"""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.core.clock import (
    ASIA,
    LONDON,
    NEW_YORK,
    OVERLAP,
    ROLLOVER,
    Clock,
    ManualClock,
    SessionCalculator,
    SystemClock,
    ensure_utc,
    local_to_utc,
)
from goldsweep.core.settings import (
    OverlapConfig,
    SessionsConfig,
    SessionWindow,
    Settings,
    TimeWindow,
)
from tests.helpers import utc

D = dt.date
Hm = tuple[int, int]


def spec_windows(settings: Settings) -> SessionsConfig:
    """The shipped windows with Asia as the spec has it: range only, no new trades.

    The owner switched Asia on (D87); these tests pin the calculator's behaviour, so
    they keep the spec's timetable (test_asia_can_be_switched_on covers Asia on).
    """
    windows = settings.config.sessions
    return windows.model_copy(update={"asia": windows.asia.model_copy(update={"trade": False})})


@pytest.fixture
def sessions(settings: Settings) -> SessionCalculator:
    return SessionCalculator(spec_windows(settings))


def calculator(settings: Settings, **changes: object) -> SessionCalculator:
    """Session calculator for the spec's windows with some session settings replaced."""
    return SessionCalculator(spec_windows(settings).model_copy(update=changes))


# --------------------------------------------------------------------------- #
# Instants and clocks
# --------------------------------------------------------------------------- #


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(dt.datetime(2026, 11, 16, 8, 0))


def test_aware_datetime_is_converted_to_utc() -> None:
    kampala = dt.timezone(dt.timedelta(hours=3))
    converted = ensure_utc(dt.datetime(2026, 11, 16, 11, 0, tzinfo=kampala))
    assert converted == utc(2026, 11, 16, 8)
    assert converted.tzinfo is dt.UTC


def test_system_clock_returns_aware_utc() -> None:
    clock = SystemClock()
    assert isinstance(clock, Clock)
    assert clock.now().utcoffset() == dt.timedelta(0)


def test_manual_clock_moves_only_when_told() -> None:
    clock = ManualClock(utc(2026, 11, 16, 8))
    assert isinstance(clock, Clock)
    assert clock.now() == utc(2026, 11, 16, 8)
    assert clock.advance(minutes=5) == utc(2026, 11, 16, 8, 5)
    assert clock.advance(dt.timedelta(hours=1)) == utc(2026, 11, 16, 9, 5)
    clock.set(utc(2026, 11, 17))
    assert clock.now() == utc(2026, 11, 17)


def test_manual_clock_cannot_go_backwards_or_take_naive_times() -> None:
    clock = ManualClock(utc(2026, 11, 16, 8))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(minutes=-1)
    with pytest.raises(ValueError, match="naive"):
        clock.set(dt.datetime(2026, 11, 16, 9))
    with pytest.raises(ValueError, match="naive"):
        ManualClock(dt.datetime(2026, 11, 16, 9))


def test_local_to_utc_handles_the_spring_gap_and_autumn_fold() -> None:
    # 02:30 does not exist in New York on 8 March 2026: read with the pre-change offset.
    assert local_to_utc(D(2026, 3, 8), dt.time(2, 30), "America/New_York") == utc(2026, 3, 8, 7, 30)
    # 01:30 happens twice on 1 November 2026: the first occurrence (still EDT) is meant.
    assert local_to_utc(D(2026, 11, 1), dt.time(1, 30), "America/New_York") == utc(
        2026, 11, 1, 5, 30
    )


# --------------------------------------------------------------------------- #
# Daylight saving: March, October and November transitions
# --------------------------------------------------------------------------- #

DST_CASES: list[tuple[str, dt.date, Hm, Hm]] = [
    # ---- 2026, March: US changes on the 8th, UK on the 29th ----
    (NEW_YORK, D(2026, 3, 6), (13, 0), (16, 0)),  # Fri before the US change (EST)
    (NEW_YORK, D(2026, 3, 9), (12, 0), (15, 0)),  # Mon after it (EDT)
    (LONDON, D(2026, 3, 9), (7, 0), (10, 0)),  # UK has not changed yet (GMT)
    (LONDON, D(2026, 3, 27), (7, 0), (10, 0)),  # Fri before the UK change
    (LONDON, D(2026, 3, 30), (6, 0), (9, 0)),  # Mon after it (BST)
    (NEW_YORK, D(2026, 3, 30), (12, 0), (15, 0)),
    # ---- 2026, October: UK changes back on the 25th ----
    (LONDON, D(2026, 10, 23), (6, 0), (9, 0)),
    (LONDON, D(2026, 10, 26), (7, 0), (10, 0)),
    (NEW_YORK, D(2026, 10, 26), (12, 0), (15, 0)),  # US still on EDT
    # ---- 2026, November: US changes back on the 1st ----
    (NEW_YORK, D(2026, 10, 30), (12, 0), (15, 0)),
    (NEW_YORK, D(2026, 11, 2), (13, 0), (16, 0)),
    (LONDON, D(2026, 11, 2), (7, 0), (10, 0)),
    # ---- Tokyo never changes ----
    (ASIA, D(2026, 3, 9), (0, 0), (6, 0)),
    (ASIA, D(2026, 7, 1), (0, 0), (6, 0)),
    (ASIA, D(2026, 11, 2), (0, 0), (6, 0)),
    # ---- the rollover block follows New York ----
    (ROLLOVER, D(2026, 3, 6), (21, 45), (23, 15)),
    (ROLLOVER, D(2026, 3, 9), (20, 45), (22, 15)),
    (ROLLOVER, D(2026, 10, 30), (20, 45), (22, 15)),
    (ROLLOVER, D(2026, 11, 2), (21, 45), (23, 15)),
    # ---- 2025 ----
    (NEW_YORK, D(2025, 3, 7), (13, 0), (16, 0)),
    (NEW_YORK, D(2025, 3, 10), (12, 0), (15, 0)),
    (LONDON, D(2025, 3, 28), (7, 0), (10, 0)),
    (LONDON, D(2025, 3, 31), (6, 0), (9, 0)),
    (LONDON, D(2025, 10, 24), (6, 0), (9, 0)),
    (LONDON, D(2025, 10, 27), (7, 0), (10, 0)),
    (NEW_YORK, D(2025, 10, 31), (12, 0), (15, 0)),
    (NEW_YORK, D(2025, 11, 3), (13, 0), (16, 0)),
    # ---- 2027 ----
    (NEW_YORK, D(2027, 3, 12), (13, 0), (16, 0)),
    (NEW_YORK, D(2027, 3, 15), (12, 0), (15, 0)),
    (LONDON, D(2027, 3, 26), (7, 0), (10, 0)),
    (LONDON, D(2027, 3, 29), (6, 0), (9, 0)),
    (LONDON, D(2027, 10, 29), (6, 0), (9, 0)),
    (LONDON, D(2027, 11, 1), (7, 0), (10, 0)),
    (NEW_YORK, D(2027, 11, 5), (12, 0), (15, 0)),
    (NEW_YORK, D(2027, 11, 8), (13, 0), (16, 0)),
]


@pytest.mark.parametrize(
    ("name", "day", "start", "end"),
    DST_CASES,
    ids=[f"{name}-{day}" for name, day, _start, _end in DST_CASES],
)
def test_windows_follow_local_time_across_dst(
    sessions: SessionCalculator, name: str, day: dt.date, start: Hm, end: Hm
) -> None:
    span = sessions.span(name, day)
    assert span.start == utc(day.year, day.month, day.day, *start)
    assert span.end == utc(day.year, day.month, day.day, *end)
    assert span.local_date == day


OVERLAP_CASES: list[tuple[dt.date, Hm, Hm]] = [
    (D(2026, 1, 15), (13, 0), (16, 30)),  # GMT + EST
    (D(2026, 3, 16), (12, 0), (16, 30)),  # GMT + EDT: the US changed first, overlap is longer
    (D(2026, 7, 15), (12, 0), (15, 30)),  # BST + EDT
    (D(2026, 10, 28), (12, 0), (16, 30)),  # GMT + EDT: the UK changed back first
    (D(2026, 11, 16), (13, 0), (16, 30)),  # GMT + EST
]


@pytest.mark.parametrize(
    ("day", "start", "end"), OVERLAP_CASES, ids=[str(case[0]) for case in OVERLAP_CASES]
)
def test_overlap_is_the_intersection_of_both_full_sessions(
    sessions: SessionCalculator, day: dt.date, start: Hm, end: Hm
) -> None:
    span = sessions.overlap(day)
    assert span is not None
    assert span.start == utc(day.year, day.month, day.day, *start)
    assert span.end == utc(day.year, day.month, day.day, *end)
    assert span == sessions.span(OVERLAP, day)


def test_every_day_of_2026_uses_the_right_offset(sessions: SessionCalculator) -> None:
    """No hard-coded offsets: the UTC hour changes exactly on the real DST dates."""
    days = [D(2026, 1, 1) + dt.timedelta(days=n) for n in range(365)]
    london_hours = [sessions.span(LONDON, day).start.hour for day in days]
    new_york_hours = [sessions.span(NEW_YORK, day).start.hour for day in days]
    assert set(london_hours) == {6, 7}
    assert set(new_york_hours) == {12, 13}
    assert london_hours.count(6) == 210  # British Summer Time: 29 Mar to 24 Oct
    assert new_york_hours.count(12) == 238  # US daylight time: 8 Mar to 31 Oct
    assert {sessions.span(ASIA, day).start.hour for day in days} == {0}


# --------------------------------------------------------------------------- #
# Windows for a date
# --------------------------------------------------------------------------- #


def test_spans_for_date_are_sorted_and_labelled(sessions: SessionCalculator) -> None:
    spans = sessions.spans_for_date(D(2026, 11, 16))
    assert [span.name for span in spans] == [ASIA, LONDON, NEW_YORK, OVERLAP, ROLLOVER]
    by_name = {span.name: span for span in spans}
    assert by_name[ASIA].trade is False
    assert by_name[LONDON].trade is True
    assert by_name[ROLLOVER].blocks_trading is True and by_name[ROLLOVER].trade is False
    assert by_name[LONDON].definition == "07:00-10:00 Europe/London"
    assert by_name[LONDON].duration == dt.timedelta(hours=3)
    assert by_name[LONDON].label == "London kill zone"


def test_span_is_start_inclusive_end_exclusive(sessions: SessionCalculator) -> None:
    span = sessions.span(LONDON, D(2026, 11, 16))
    assert span.contains(utc(2026, 11, 16, 7, 0))
    assert span.contains(utc(2026, 11, 16, 9, 59, 59))
    assert not span.contains(utc(2026, 11, 16, 10, 0))
    assert not span.contains(utc(2026, 11, 16, 6, 59, 59))


def test_unknown_session_name(sessions: SessionCalculator) -> None:
    with pytest.raises(KeyError):
        sessions.span("sydney", D(2026, 11, 16))


def test_sessions_that_never_overlap(settings: Settings) -> None:
    calc = calculator(
        settings,
        overlap=OverlapConfig(
            london=TimeWindow(tz="Europe/London", start="08:00", end="10:00"),
            newyork=TimeWindow(tz="America/New_York", start="08:00", end="17:00"),
        ),
    )
    assert calc.overlap(D(2026, 11, 16)) is None
    assert OVERLAP not in [span.name for span in calc.spans_for_date(D(2026, 11, 16))]
    with pytest.raises(ValueError, match="do not overlap"):
        calc.span(OVERLAP, D(2026, 11, 16))


def test_window_crossing_local_midnight(settings: Settings) -> None:
    calc = calculator(
        settings,
        rollover_block=TimeWindow(tz="America/New_York", start="22:00", end="02:00"),
    )
    span = calc.span(ROLLOVER, D(2026, 11, 16))
    assert span.start == utc(2026, 11, 17, 3, 0)  # Mon 22:00 EST
    assert span.end == utc(2026, 11, 17, 7, 0)  # Tue 02:00 EST
    inside = utc(2026, 11, 17, 6, 0)  # Tue 01:00 in New York: belongs to Monday's window
    assert ROLLOVER in calc.status(inside).active
    assert not calc.status(inside).allowed


# --------------------------------------------------------------------------- #
# Is a new trade allowed right now? (Monday 16 November 2026, winter time)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("moment", "allowed", "session"),
    [
        (utc(2026, 11, 16, 3, 0), False, None),  # Asia builds the range, trading off
        (utc(2026, 11, 16, 6, 59), False, None),
        (utc(2026, 11, 16, 7, 0), True, LONDON),
        (utc(2026, 11, 16, 9, 59), True, LONDON),
        (utc(2026, 11, 16, 10, 0), False, None),
        (utc(2026, 11, 16, 11, 0), False, None),
        (utc(2026, 11, 16, 13, 30), True, NEW_YORK),  # kill zone outranks the overlap
        (utc(2026, 11, 16, 16, 15), True, OVERLAP),
        (utc(2026, 11, 16, 16, 30), False, None),
        (utc(2026, 11, 16, 22, 0), False, None),  # rollover block
    ],
)
def test_status_through_a_trading_day(
    sessions: SessionCalculator, moment: dt.datetime, allowed: bool, session: str | None
) -> None:
    status = sessions.status(moment)
    assert status.allowed is allowed
    assert status.session == session
    assert bool(status.reasons) is not allowed


def test_status_lists_active_windows_and_reasons(sessions: SessionCalculator) -> None:
    overlap_and_kill_zone = sessions.status(utc(2026, 11, 16, 13, 30))
    assert set(overlap_and_kill_zone.active) == {NEW_YORK, OVERLAP}
    assert overlap_and_kill_zone.reasons == ()

    asia = sessions.status(utc(2026, 11, 16, 3, 0))
    assert asia.active == (ASIA,)
    assert asia.reasons == ("outside the tradeable session windows",)

    rollover = sessions.status(utc(2026, 11, 16, 22, 0))
    assert ROLLOVER in rollover.active
    assert "rollover block: no new trades" in rollover.reasons


def test_status_rejects_naive_time(sessions: SessionCalculator) -> None:
    with pytest.raises(ValueError, match="naive"):
        sessions.status(dt.datetime(2026, 11, 16, 8, 0))


def test_asia_can_be_switched_on(settings: Settings) -> None:
    asia_on = settings.config.sessions.asia.model_copy(update={"trade": True})
    status = calculator(settings, asia=asia_on).status(utc(2026, 11, 16, 3, 0))
    assert status.allowed and status.session == ASIA


def test_overlap_can_be_switched_off(settings: Settings) -> None:
    overlap_off = settings.config.sessions.overlap.model_copy(update={"trade": False})
    status = calculator(settings, overlap=overlap_off).status(utc(2026, 11, 16, 16, 15))
    assert not status.allowed and status.session is None


# --------------------------------------------------------------------------- #
# Friday cutoff and the weekend
# --------------------------------------------------------------------------- #


def test_friday_cutoff_is_noon_new_york(sessions: SessionCalculator) -> None:
    assert not sessions.after_friday_cutoff(utc(2026, 11, 20, 16, 59))  # Fri 11:59 EST
    assert sessions.after_friday_cutoff(utc(2026, 11, 20, 17, 0))  # Fri 12:00 EST
    assert sessions.after_friday_cutoff(utc(2026, 11, 20, 23, 0))
    assert not sessions.after_friday_cutoff(utc(2026, 11, 19, 18, 0))  # Thursday
    assert sessions.after_friday_cutoff(utc(2026, 7, 17, 16, 0))  # summer: 12:00 EDT
    assert not sessions.after_friday_cutoff(utc(2026, 7, 17, 15, 59))
    assert sessions.friday_cutoff_instant(D(2026, 11, 20)) == utc(2026, 11, 20, 17, 0)
    assert sessions.friday_cutoff_instant(D(2026, 7, 17)) == utc(2026, 7, 17, 16, 0)
    assert sessions.friday_cutoff_definition == "from 12:00 America/New_York"


def test_friday_morning_kill_zone_is_still_tradeable(sessions: SessionCalculator) -> None:
    status = sessions.status(utc(2026, 11, 20, 14, 0))  # Fri 09:00 EST
    assert status.allowed and status.session == NEW_YORK


def test_friday_cutoff_blocks_a_window_that_runs_past_noon(settings: Settings) -> None:
    long_new_york = SessionWindow(tz="America/New_York", start="08:00", end="14:00", trade=True)
    calc = calculator(settings, newyork=long_new_york)
    before = calc.status(utc(2026, 11, 20, 16, 45))  # Fri 11:45 EST
    after = calc.status(utc(2026, 11, 20, 17, 30))  # Fri 12:30 EST
    assert before.allowed
    assert not after.allowed
    assert after.session == NEW_YORK  # inside the window, but blocked
    assert any("Friday cutoff" in reason for reason in after.reasons)
    thursday = calc.status(utc(2026, 11, 19, 17, 30))
    assert thursday.allowed


def test_weekend_runs_from_friday_close_to_sunday_open(sessions: SessionCalculator) -> None:
    assert not sessions.is_weekend(utc(2026, 11, 13, 21, 59))  # Fri 16:59 EST
    assert sessions.is_weekend(utc(2026, 11, 13, 22, 0))  # Fri 17:00 EST
    assert sessions.is_weekend(utc(2026, 11, 14, 12, 0))  # Saturday
    assert sessions.is_weekend(utc(2026, 11, 15, 22, 59))  # Sun 17:59 EST
    assert not sessions.is_weekend(utc(2026, 11, 15, 23, 0))  # Sun 18:00 EST
    assert not sessions.is_weekend(utc(2026, 11, 18, 12, 0))  # Wednesday
    # Summer: the same New York times are one hour earlier in UTC.
    assert sessions.is_weekend(utc(2026, 7, 10, 21, 0))  # Fri 17:00 EDT
    assert not sessions.is_weekend(utc(2026, 7, 12, 22, 0))  # Sun 18:00 EDT


def test_next_market_open(sessions: SessionCalculator) -> None:
    assert sessions.next_market_open(utc(2026, 11, 13, 23, 0)) == utc(2026, 11, 15, 23, 0)
    assert sessions.next_market_open(utc(2026, 11, 14, 12, 0)) == utc(2026, 11, 15, 23, 0)
    assert sessions.next_market_open(utc(2026, 11, 15, 20, 0)) == utc(2026, 11, 15, 23, 0)
    assert sessions.next_market_open(utc(2026, 7, 11, 12, 0)) == utc(2026, 7, 12, 22, 0)
    assert sessions.next_market_open(utc(2026, 11, 16, 8, 0)) is None


def test_no_trading_on_the_weekend(sessions: SessionCalculator) -> None:
    status = sessions.status(utc(2026, 11, 14, 8, 0))  # Saturday, inside the London clock window
    assert not status.allowed
    assert "weekend: market closed" in status.reasons
    assert status.session == LONDON


def test_next_tradeable_window(sessions: SessionCalculator) -> None:
    saturday = sessions.next_tradeable_span(utc(2026, 11, 14, 12, 0))
    assert saturday is not None
    assert (saturday.name, saturday.start) == (LONDON, utc(2026, 11, 16, 7, 0))

    inside_london = sessions.next_tradeable_span(utc(2026, 11, 16, 8, 0))
    assert inside_london is not None
    assert (inside_london.name, inside_london.start) == (NEW_YORK, utc(2026, 11, 16, 13, 0))

    friday_afternoon = sessions.next_tradeable_span(utc(2026, 11, 20, 17, 30))
    assert friday_afternoon is not None
    assert (friday_afternoon.name, friday_afternoon.start) == (LONDON, utc(2026, 11, 23, 7, 0))


def test_next_tradeable_window_when_nothing_is_tradeable(settings: Settings) -> None:
    sessions_cfg = settings.config.sessions
    calc = calculator(
        settings,
        london=sessions_cfg.london.model_copy(update={"trade": False}),
        newyork=sessions_cfg.newyork.model_copy(update={"trade": False}),
        overlap=sessions_cfg.overlap.model_copy(update={"trade": False}),
    )
    assert calc.next_tradeable_span(utc(2026, 11, 16, 8, 0)) is None


def test_window_squeezed_into_a_dst_change_is_empty_not_negative(settings: Settings) -> None:
    """02:30 does not exist on 8 March 2026 in New York; such a window must not run backwards."""
    calc = calculator(
        settings,
        rollover_block=TimeWindow(tz="America/New_York", start="02:30", end="03:15"),
    )
    span = calc.span(ROLLOVER, D(2026, 3, 8))
    assert span.duration == dt.timedelta(0)
    assert not span.contains(span.start)


def test_trading_at_all_hours_keeps_the_blocks(settings: Settings) -> None:
    """sessions.trade_all_hours: any hour the market is open, but never in the blocks."""
    all_hours = SessionCalculator(
        settings.config.sessions.model_copy(update={"trade_all_hours": True})
    )
    between = utc(2026, 11, 18, 11, 30)  # between the London and New York kill zones
    assert not SessionCalculator(settings.config.sessions).status(between).allowed
    status = all_hours.status(between)
    assert status.allowed and status.session is None  # no kill-zone label, no kill-zone points
    assert all_hours.status(utc(2026, 11, 18, 8, 0)).session == "london"
    assert not all_hours.status(utc(2026, 11, 18, 22, 0)).allowed  # rollover block
    assert not all_hours.status(utc(2026, 11, 20, 18, 0)).allowed  # Friday cutoff
    assert not all_hours.status(utc(2026, 11, 21, 12, 0)).allowed  # weekend
