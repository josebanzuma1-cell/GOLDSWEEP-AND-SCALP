"""Broker server time <-> UTC conversion rules."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from goldsweep.core.server_time import (
    DEFAULT_ASSUMED_RULE,
    ServerTimeRule,
    format_offset,
    offset_from_tick,
    rule_for_offset,
)
from tests.helpers import utc

NEW_YORK = ZoneInfo("America/New_York")
NY7 = ServerTimeRule("America/New_York", dt.timedelta(hours=7))
ATHENS = ServerTimeRule("Europe/Athens")


def hours(value: float) -> dt.timedelta:
    return dt.timedelta(hours=value)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "zone", "shift_minutes"),
    [
        ("UTC", "UTC", 0),
        ("+02:00", "UTC", 120),
        ("UTC+2", "UTC", 120),
        ("utc+3", "UTC", 180),
        ("GMT-5", "UTC", -300),
        ("+0530", "UTC", 330),
        (" +02:00 ", "UTC", 120),
        ("Europe/Athens", "Europe/Athens", 0),
        ("NY+7", "America/New_York", 420),
        ("ny+7", "America/New_York", 420),
        ("America/New_York+7", "America/New_York", 420),
        ("America/New_York+07:00", "America/New_York", 420),
    ],
)
def test_parse(spec: str, zone: str, shift_minutes: int) -> None:
    rule = ServerTimeRule.parse(spec)
    assert rule.zone == zone
    assert rule.shift == dt.timedelta(minutes=shift_minutes)


@pytest.mark.parametrize("spec", ["", "   ", "auto", "Mars/Olympus", "+25:00", "+02:75", "two"])
def test_parse_rejects_nonsense(spec: str) -> None:
    with pytest.raises(ValueError):
        ServerTimeRule.parse(spec)


@pytest.mark.parametrize(
    "rule",
    [
        ServerTimeRule(),
        ServerTimeRule("UTC", hours(2)),
        ServerTimeRule("UTC", hours(-5)),
        ATHENS,
        NY7,
    ],
)
def test_spec_round_trips(rule: ServerTimeRule) -> None:
    assert ServerTimeRule.parse(rule.spec) == rule


def test_spec_and_description_text() -> None:
    assert ServerTimeRule().spec == "UTC"
    assert ServerTimeRule("UTC", hours(2)).spec == "UTC+02:00"
    assert NY7.spec == "America/New_York+07:00"
    assert NY7.describe() == "NY+7 (New York close)"
    assert ATHENS.describe() == "Europe/Athens"
    assert DEFAULT_ASSUMED_RULE == NY7


def test_unknown_zone_and_oversized_shift_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown IANA time zone"):
        ServerTimeRule("Mars/Olympus")
    with pytest.raises(ValueError, match="14 hours"):
        ServerTimeRule("UTC", hours(15))


@pytest.mark.parametrize(
    ("offset", "text"),
    [(hours(2), "+02:00"), (hours(-5), "-05:00"), (hours(5.5), "+05:30"), (hours(0), "+00:00")],
)
def test_format_offset(offset: dt.timedelta, text: str) -> None:
    assert format_offset(offset) == text


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


def test_new_york_close_server_follows_us_daylight_saving() -> None:
    assert NY7.utc_offset(utc(2026, 1, 15, 12)) == hours(2)  # winter
    assert NY7.utc_offset(utc(2026, 7, 15, 12)) == hours(3)  # summer
    # 16 March: the US has changed, Europe has not. The two conventions disagree.
    assert NY7.utc_offset(utc(2026, 3, 16, 12)) == hours(3)
    assert ATHENS.utc_offset(utc(2026, 3, 16, 12)) == hours(2)
    # 28 October: Europe has changed back, the US has not.
    assert NY7.utc_offset(utc(2026, 10, 28, 12)) == hours(3)
    assert ATHENS.utc_offset(utc(2026, 10, 28, 12)) == hours(2)


@pytest.mark.parametrize(
    "day", [dt.date(2026, 1, 15), dt.date(2026, 3, 17), dt.date(2026, 7, 15), dt.date(2026, 10, 28)]
)
def test_new_york_close_server_midnight_is_always_5pm_new_york(day: dt.date) -> None:
    server_midnight = dt.datetime(day.year, day.month, day.day, 0, 0)
    new_york = NY7.to_utc(server_midnight).astimezone(NEW_YORK)
    assert (new_york.hour, new_york.minute) == (17, 0)
    assert new_york.date() == day - dt.timedelta(days=1)


def test_fixed_offset_and_utc_rules() -> None:
    fixed = ServerTimeRule("UTC", hours(2))
    assert fixed.to_utc(dt.datetime(2026, 11, 16, 12, 0)) == utc(2026, 11, 16, 10, 0)
    assert fixed.from_utc(utc(2026, 11, 16, 10, 0)) == dt.datetime(2026, 11, 16, 12, 0)
    assert fixed.utc_offset(utc(2026, 7, 1)) == hours(2)  # no daylight saving
    plain = ServerTimeRule()
    assert plain.to_utc(dt.datetime(2026, 11, 16, 12, 0)) == utc(2026, 11, 16, 12, 0)
    assert plain.utc_offset(utc(2026, 7, 1)) == hours(0)


@pytest.mark.parametrize("rule", [ServerTimeRule(), ServerTimeRule("UTC", hours(2)), ATHENS, NY7])
@pytest.mark.parametrize("moment", [utc(2026, 1, 15, 9, 35), utc(2026, 7, 15, 21, 5)])
def test_to_utc_and_from_utc_are_inverses(rule: ServerTimeRule, moment: dt.datetime) -> None:
    server = rule.from_utc(moment)
    assert server.tzinfo is None
    assert rule.to_utc(server) == moment


def test_to_utc_rejects_aware_input_and_from_utc_rejects_naive_input() -> None:
    with pytest.raises(ValueError, match="naive"):
        NY7.to_utc(utc(2026, 11, 16, 12))
    with pytest.raises(ValueError, match="naive"):
        NY7.from_utc(dt.datetime(2026, 11, 16, 12))


@pytest.mark.parametrize("rule", [ServerTimeRule(), ServerTimeRule("UTC", hours(2)), ATHENS, NY7])
def test_index_conversion_matches_the_scalar_one(rule: ServerTimeRule) -> None:
    """Hourly server stamps on the trading days around the US change of 8 March 2026."""
    stamps = pd.date_range("2026-03-05 00:00", "2026-03-11 00:00", freq="1h")
    stamps = stamps[stamps.day_of_week < 5]  # DST switches happen on Sunday, market shut
    converted = rule.index_to_utc(stamps)
    assert converted.tz is not None
    expected = [rule.to_utc(stamp.to_pydatetime()) for stamp in stamps]
    assert list(converted) == expected


def test_index_conversion_rejects_an_aware_index() -> None:
    aware = pd.date_range("2026-03-05", periods=3, freq="1h", tz="UTC")
    with pytest.raises(ValueError, match="naive"):
        NY7.index_to_utc(aware)


def test_index_conversion_survives_the_weekend_dst_gap() -> None:
    """Stamps inside the skipped or repeated hour must not crash the conversion."""
    gap = pd.DatetimeIndex(["2026-03-08 09:30:00"])  # 02:30 New York does not exist
    fold = pd.DatetimeIndex(["2026-11-01 08:30:00"])  # 01:30 New York happens twice
    assert len(NY7.index_to_utc(gap)) == 1
    assert len(NY7.index_to_utc(fold)) == 1


# --------------------------------------------------------------------------- #
# Measuring the offset from a tick
# --------------------------------------------------------------------------- #

NOW = utc(2026, 11, 16, 10, 2, 30)


def test_offset_from_a_live_tick() -> None:
    result = offset_from_tick(NOW.timestamp() + 2 * 3600, NOW)
    assert result is not None
    offset, skew = result
    assert offset == hours(2)
    assert skew == pytest.approx(0.0)


def test_offset_tolerates_clock_skew_and_reports_it() -> None:
    result = offset_from_tick(NOW.timestamp() + 3 * 3600 + 12.5, NOW)
    assert result is not None
    assert result[0] == hours(3)
    assert result[1] == pytest.approx(12.5)
    behind = offset_from_tick(NOW.timestamp() - 40.0, NOW)
    assert behind is not None
    assert behind[0] == hours(0)
    assert behind[1] == pytest.approx(-40.0)


def test_offset_supports_half_hour_zones() -> None:
    result = offset_from_tick(NOW.timestamp() + 5.5 * 3600, NOW)
    assert result is not None and result[0] == hours(5.5)


def test_offset_rejects_ticks_that_cannot_be_live() -> None:
    assert offset_from_tick(NOW.timestamp() + 2 * 3600 - 13 * 60, NOW) is None  # 13 min off
    assert offset_from_tick(NOW.timestamp() - 20 * 3600, NOW) is None  # beyond UTC-12
    assert offset_from_tick(NOW.timestamp() + 20 * 3600, NOW) is None  # beyond UTC+14


def test_offset_needs_an_aware_clock_reading() -> None:
    with pytest.raises(ValueError, match="naive"):
        offset_from_tick(0.0, dt.datetime(2026, 11, 16, 10, 0))


@pytest.mark.parametrize(
    ("offset", "at", "spec"),
    [
        (hours(2), utc(2026, 1, 15, 12), "America/New_York+07:00"),
        (hours(3), utc(2026, 7, 15, 12), "America/New_York+07:00"),
        (hours(0), utc(2026, 7, 15, 12), "UTC"),
        (hours(2), utc(2026, 7, 15, 12), "UTC+02:00"),  # +2 in summer is not New York close
        (hours(3), utc(2026, 1, 15, 12), "UTC+03:00"),
        (hours(8), utc(2026, 1, 15, 12), "UTC+08:00"),
    ],
)
def test_rule_chosen_for_a_measured_offset(
    offset: dt.timedelta, at: dt.datetime, spec: str
) -> None:
    assert rule_for_offset(offset, at).spec == spec
