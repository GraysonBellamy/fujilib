"""Samples and wide rows: one fixed key set for success and error rows (design §7.6)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fujilib.devices.models import READING_COLUMNS
from fujilib.errors import FujiModbusTimeoutError
from fujilib.protocol.base import ProtocolKind
from fujilib.registry.channels import ChannelId
from fujilib.sinks.base import HEADER_COLUMNS, row_columns, sample_to_row
from fujilib.streaming.sample import Sample
from tests.factories import MONO0, T0, frame, timing

CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)


def ok_sample() -> Sample:
    return Sample.from_frame(frame(), device="fuji", address=1, metadata={"mode": "poll"})


def error_sample() -> Sample:
    return Sample.from_error(
        FujiModbusTimeoutError("no reply"),
        device="fuji",
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        timing=timing(1000.0, latency_ms=500.0),
    )


def test_sample_is_timed_by_the_concentration_block() -> None:
    sample = ok_sample()
    assert sample.t_mono_ns == MONO0 + 10_000_000
    assert sample.t_utc == T0 + timedelta(milliseconds=10)
    assert sample.t_utc == sample.requested_at + (sample.received_at - sample.requested_at) / 2
    assert sample.latency_s == pytest.approx(0.020)
    assert sample.t_midpoint_mono_ns is None
    assert sample.t_utc.tzinfo is not None
    assert sample.metadata == {"mode": "poll"}
    assert sample.error is None


def test_error_sample_keeps_the_attempt_timing() -> None:
    sample = error_sample()
    assert sample.frame is None
    assert isinstance(sample.error, FujiModbusTimeoutError)
    assert sample.latency_s == pytest.approx(0.5)
    assert sample.protocol is ProtocolKind.MODBUS_RTU


def test_row_layout() -> None:
    row = sample_to_row(ok_sample(), CHANNELS)
    header = [c.name for c in HEADER_COLUMNS]
    assert list(row)[: len(header)] == header
    assert row["ch3_value"] == 20.29
    assert row["ch3_gas"] == "o2"
    assert row["ch3_state"] == "ok"
    assert row["ch1_raw"] == -11
    assert row["t_utc"] == (T0 + timedelta(milliseconds=10)).isoformat()
    assert row["protocol"] == "modbus_rtu"
    assert row["alarm1"] == "none"
    assert row["error_type"] is None
    assert row["error_message"] is None
    assert "mode" not in row  # metadata stays out of rows


def test_success_and_error_rows_have_the_same_keys() -> None:
    ok = sample_to_row(ok_sample(), CHANNELS)
    err = sample_to_row(error_sample(), CHANNELS)
    assert list(ok) == list(err)
    assert err["error_type"] == "fujilib.errors.FujiModbusTimeoutError"
    assert err["error_message"] == "no reply"
    for channel in CHANNELS:
        prefix = f"ch{channel.number}_"
        assert all(err[prefix + c.name] is None for c in READING_COLUMNS)
    assert err["instrument_error"] is None


def test_row_columns_match_the_rows() -> None:
    specs = row_columns(CHANNELS)
    row = sample_to_row(ok_sample(), CHANNELS)
    assert [s.name for s in specs] == list(row)
    for spec in specs:
        value = row[spec.name]
        if value is None:
            assert spec.nullable, spec.name
        else:
            # bool is an int subclass; the declared type must match exactly.
            assert type(value) is spec.python_type, spec.name


def test_values_are_scalars_only() -> None:
    for sample in (ok_sample(), error_sample()):
        for key, value in sample_to_row(sample, CHANNELS).items():
            assert value is None or type(value) in {float, int, str, bool}, key


def test_value_columns_are_always_float() -> None:
    whole = frame().channel("CH1")
    assert isinstance(whole.value, float)
    row = sample_to_row(ok_sample(), CHANNELS)
    assert all(type(row[f"ch{n}_value"]) is float for n in (1, 2, 3))


def test_channels_default_to_the_frame() -> None:
    assert list(sample_to_row(ok_sample())) == list(sample_to_row(ok_sample(), CHANNELS))
    assert not any(k.startswith("ch") for k in sample_to_row(error_sample()))


def test_a_channel_missing_from_the_frame_is_none() -> None:
    row = sample_to_row(ok_sample(), (*CHANNELS, ChannelId.CH4))
    assert row["ch4_value"] is None
