"""Frozen models: validity, timing, lookup, rows and partial timestamps (design §8)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fujilib.devices.models import (
    ANALYZER_COLUMNS,
    READING_COLUMNS,
    PartialTimestamp,
    RangeInfo,
    ReadingState,
    encode_codes,
    encode_enum,
)
from fujilib.errors import FujiValidationError
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.enums import AlarmState, ErrorCode
from fujilib.registry.units import Unit
from tests.factories import MONO0, T0, analyzer, frame, reading, status, timing


@pytest.mark.parametrize(
    ("state", "valid"),
    [
        (ReadingState.OK, True),
        (ReadingState.UNKNOWN, None),
        (ReadingState.HOLD, False),
        (ReadingState.CALIBRATING, False),
        (ReadingState.AUTO_CALIBRATION, False),
        (ReadingState.CHANNEL_ERROR, False),
        (ReadingState.ANALYZER_ERROR, False),
        (ReadingState.SOURCE_INVALID, False),
    ],
)
def test_validity_derives_from_state(state: ReadingState, valid: bool | None) -> None:
    assert state.valid is valid
    assert reading(ChannelId.CH1, Gas.CO2, 1, 0, state=state).valid is valid


def test_calibrating_covers_manual_and_automatic() -> None:
    assert not status().calibrating
    assert status(zero=True).calibrating
    assert status(span=True).calibrating
    auto = dataclasses.replace(status(), auto_zero_running=True)
    assert auto.calibrating


def test_transfer_timing() -> None:
    t = timing(0.0, latency_ms=20.0)
    assert t.latency_s == pytest.approx(0.020)
    assert t.midpoint_mono_ns == MONO0 + 10_000_000
    assert t.midpoint_utc == T0 + timedelta(milliseconds=10)


def test_reading_exact_decimal() -> None:
    o2 = frame().channel("ch3")
    assert o2.value == 20.29
    assert o2.as_decimal() == Decimal("20.29")
    assert dataclasses.replace(o2, value=None).as_decimal() is None


def test_frame_channel_lookup() -> None:
    f = frame()
    assert f.channel(ChannelId.CH2).raw_value == -9
    assert f.channels == (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)
    with pytest.raises(FujiValidationError):
        f.channel("CH4")
    with pytest.raises(FujiValidationError):
        f.channel("CH99")


def test_reading_columns_are_scalars_in_order() -> None:
    row = frame().channel("CH1").as_dict()
    assert list(row) == [c.name for c in READING_COLUMNS]
    assert row["value"] == -0.11
    assert row["raw"] == -11
    assert row["unit"] == "vol%"
    assert row["gas"] == "co2"
    assert row["state"] == "ok"
    assert row["valid"] is True
    assert row["hold"] is False
    assert row["errors"] == ""
    for value in row.values():
        assert value is None or isinstance(value, float | int | str | bool)


def test_reading_columns_without_status() -> None:
    derived = dataclasses.replace(frame().channel("CH1"), status=None, state=ReadingState.UNKNOWN)
    row = derived.as_dict()
    assert row["hold"] is None
    assert row["calibrating"] is None
    assert row["errors"] is None
    assert row["valid"] is None


def test_analyzer_columns() -> None:
    alarms = (
        AlarmState.HIGH,
        AlarmState.NONE,
        AlarmState.NONE,
        AlarmState.NONE,
        AlarmState.NONE,
        12,
    )
    row = analyzer(errors=(ErrorCode.DETECTOR, ErrorCode.LIGHT_SOURCE), alarms=alarms).as_dict()
    assert list(row) == [c.name for c in ANALYZER_COLUMNS]
    assert row["analyzer_errors"] == "1,2"
    assert row["alarm1"] == "high"
    assert row["alarm6"] == "12"  # an undocumented value is kept
    short = analyzer(alarms=(AlarmState.NONE,)).as_dict()
    assert short["alarm2"] is None


def test_long_rows_repeat_the_analyzer_status() -> None:
    rows = frame().as_long_rows(device="fuji", address=1)
    assert [r["channel"] for r in rows] == ["CH1", "CH2", "CH3"]
    assert all(r["instrument_error"] is False for r in rows)
    assert all(r["device"] == "fuji" and r["address"] == 1 for r in rows)
    readings_only = frame(detail=False).as_long_rows(device="fuji", address=1)
    assert all(r["instrument_error"] is None for r in readings_only)


def test_encoders() -> None:
    assert encode_enum(None) is None
    assert encode_enum(AlarmState.LOW_LOW) == "low_low"
    assert encode_enum(7) == "7"
    assert encode_codes(None) is None
    assert encode_codes([]) == ""
    assert encode_codes({ErrorCode.SPAN_OUT_OF_RANGE, ErrorCode.ZERO_OUT_OF_RANGE}) == "4,6"


def test_range_info() -> None:
    o2 = RangeInfo(ChannelId.CH3, 2, (Unit.VOL_PERCENT,) * 2, (21.0, 25.0), (2, 2))
    assert o2.of(2) == (Unit.VOL_PERCENT, 25.0, 2)
    with pytest.raises(FujiValidationError):
        o2.of(3)


# --- Partial timestamps ---------------------------------------------------------------

CLOCK = datetime(2026, 9, 28, 11, 51, 39)  # the bench clock, naive local time


@pytest.mark.parametrize(
    ("stamp", "max_age", "expected"),
    [
        # The bench's newest error: day 6, 15:49, no month.
        (PartialTimestamp(None, 6, 15, 49), timedelta(days=40), datetime(2026, 9, 6, 15, 49)),
        # Ambiguous: two months' 6th fall inside 70 days.
        (PartialTimestamp(None, 6, 15, 49), timedelta(days=70), None),
        # With a month it is unique across the window.
        (PartialTimestamp(8, 6, 15, 49), timedelta(days=70), datetime(2026, 8, 6, 15, 49)),
        # Later today than the clock: last year's is outside the window.
        (PartialTimestamp(9, 28, 12, 0), timedelta(days=30), None),
        # Across a year boundary.
        (PartialTimestamp(12, 31, 23, 0), timedelta(days=300), datetime(2025, 12, 31, 23, 0)),
        # A day no month has.
        (PartialTimestamp(2, 30, 0, 0), timedelta(days=400), None),
    ],
)
def test_partial_timestamp_resolve(
    stamp: PartialTimestamp, max_age: timedelta, expected: datetime | None
) -> None:
    assert stamp.resolve(CLOCK, max_age=max_age) == expected
