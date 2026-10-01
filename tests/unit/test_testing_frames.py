"""The public model builders of ``fujilib.testing.frames`` (design §10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fujilib.testing
from fujilib import ChannelId, Gas, ReadingState, Sample, sample_to_row
from fujilib.protocol.base import ProtocolKind
from fujilib.registry.channels import ChannelRole, LabelSource
from fujilib.registry.enums import AlarmState, DisplayScreen, ErrorCode, ManualCalibrationStep
from fujilib.registry.units import Unit
from fujilib.sinks.base import row_columns
from fujilib.testing import frames
from fujilib.testing.frames import (
    MONO0,
    T0,
    analyzer,
    bench_readings,
    frame,
    reading,
    status,
    timing,
)

BUILDERS = ("analyzer", "bench_readings", "frame", "reading", "status", "timing")


def test_the_package_exports_the_builders() -> None:
    assert set(frames.__all__) == {*BUILDERS, "MONO0", "T0"}
    for name in BUILDERS:
        assert name in fujilib.testing.__all__
        assert getattr(fujilib.testing, name) is getattr(frames, name)


# --- timing ------------------------------------------------------------------------------


def test_timing_defaults_to_the_fixed_origins() -> None:
    t = timing()
    assert t.requested_at == T0
    assert t.received_at == T0 + timedelta(milliseconds=20)
    assert t.t_request_mono_ns == MONO0
    assert t.t_reply_mono_ns == MONO0 + 20_000_000
    assert t.latency_s == 0.02


def test_timing_offsets_both_clocks() -> None:
    t = timing(1500.0, latency_ms=10.0)
    assert t.requested_at == T0 + timedelta(seconds=1.5)
    assert t.t_request_mono_ns == MONO0 + 1_500_000_000
    assert t.midpoint_mono_ns == MONO0 + 1_505_000_000


def test_timing_takes_a_callers_clock() -> None:
    origin = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    t = timing(250.0, origin=origin, mono_origin_ns=7)
    assert t.requested_at == origin + timedelta(milliseconds=250)
    assert t.t_request_mono_ns == 250_000_007
    assert t.t_reply_mono_ns == 270_000_007


# --- status, reading, analyzer -------------------------------------------------------------


def test_status_is_live_on_range_1_by_default() -> None:
    s = status()
    assert (s.range, s.hold, s.calibrating, s.errors) == (1, False, False, frozenset())


def test_status_takes_the_flags() -> None:
    s = status(rng=2, hold=True, zero=True, span=True, errors=(ErrorCode(5),))
    assert s.range == 2
    assert s.hold
    assert s.zero_calibrating
    assert s.span_calibrating
    assert not s.auto_zero_running
    assert not s.auto_span_running
    assert s.errors == {ErrorCode(5)}


def test_reading_scales_the_raw_value() -> None:
    r = reading(ChannelId.CH3, Gas.O2, 2095, 2)
    assert r.value == 20.95
    assert (r.raw_value, r.decimals, r.unit) == (2095, 2, Unit.VOL_PERCENT)
    assert (r.gas, r.suggested_gas) == (Gas.O2, Gas.O2)
    assert r.label_source is LabelSource.ASSERTED
    assert r.role is ChannelRole.INSTANTANEOUS
    assert r.state is ReadingState.OK
    assert r.status == status()
    assert r.protocol is ProtocolKind.MODBUS_RTU


def test_reading_takes_the_state_and_status_as_given() -> None:
    held = status(hold=True)
    r = reading(
        ChannelId.CH1,
        Gas.CO,
        125,
        0,
        unit=Unit.PPM,
        state=ReadingState.HOLD,
        channel_status=held,
        label_source=LabelSource.TYPE_CODE,
        role=ChannelRole.O2_CORRECTED,
    )
    assert r.value == 125.0
    assert r.unit is Unit.PPM
    assert r.status is held
    assert r.state is ReadingState.HOLD
    assert r.valid is False
    assert r.label_source is LabelSource.TYPE_CODE
    assert r.role is ChannelRole.O2_CORRECTED
    # The state is the caller's: a live status does not make a held state live.
    assert reading(ChannelId.CH1, Gas.CO, 1, 0, state=ReadingState.HOLD).status == status()


def test_analyzer_is_quiet_on_the_measurement_screen_by_default() -> None:
    a = analyzer()
    assert not a.instrument_error
    assert not a.calibration_error
    assert a.errors == frozenset()
    assert a.alarms == (AlarmState.NONE,) * 6
    assert not a.auto_calibration_running
    assert a.display is not None
    assert a.display.screen is DisplayScreen.MEASUREMENT
    assert a.display.calibration_step is ManualCalibrationStep.NONE


def test_analyzer_takes_the_flags() -> None:
    alarms = (AlarmState.NONE, 7, *(AlarmState.NONE,) * 4)
    a = analyzer(
        instrument_error=True, errors=(ErrorCode(1),), alarms=alarms, auto_calibration=True
    )
    assert a.instrument_error
    assert a.errors == {ErrorCode(1)}
    assert a.alarms == alarms
    assert a.auto_calibration_running


# --- frame -------------------------------------------------------------------------------


def test_bench_readings_are_the_bench_channels() -> None:
    readings = bench_readings()
    assert [(r.channel, r.gas, r.value) for r in readings] == [
        (ChannelId.CH1, Gas.CO2, -0.11),
        (ChannelId.CH2, Gas.CO, -0.009),
        (ChannelId.CH3, Gas.O2, 20.29),
    ]


def test_frame_defaults_to_a_full_bench_poll() -> None:
    f = frame()
    assert f.readings == bench_readings()
    assert f.analyzer == analyzer()
    assert f.protocol is ProtocolKind.MODBUS_RTU
    assert f.readings_timing == timing(0.0)
    assert f.status_timing == timing(25.0)
    assert len(f.raw) == 242


def test_frame_without_detail_has_no_status() -> None:
    f = frame(detail=False, status_block=analyzer(instrument_error=True), status_timing=timing(9.0))
    assert f.analyzer is None
    assert f.status_timing is None


def test_frame_takes_readings_status_and_timings() -> None:
    o2 = reading(ChannelId.CH3, Gas.O2, 2095, 2)
    block = analyzer(instrument_error=True)
    f = frame((o2,), block, readings_timing=timing(1000.0), status_timing=timing(1030.0))
    assert f.readings == (o2,)
    assert f.analyzer is block
    assert f.readings_timing == timing(1000.0)
    assert f.status_timing == timing(1030.0)


def test_a_built_frame_times_its_sample_from_the_callers_clock() -> None:
    origin = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    t = timing(0.0, origin=origin, mono_origin_ns=1_000)
    sample = Sample.from_frame(frame(readings_timing=t), device="zpa", address=1)
    assert sample.t_mono_ns == 10_001_000
    assert sample.t_utc == origin + timedelta(milliseconds=10)
    assert sample.requested_at == origin


def test_a_built_frame_makes_a_recordings_row() -> None:
    held_o2 = reading(
        ChannelId.CH3, Gas.O2, 2095, 2, channel_status=status(hold=True), state=ReadingState.HOLD
    )
    sample = Sample.from_frame(frame((held_o2,)), device="zpa", address=1)
    row = sample_to_row(sample)
    assert list(row) == [c.name for c in row_columns((ChannelId.CH3,))]
    assert row["ch3_value"] == 20.95
    assert row["ch3_state"] == "hold"
    assert row["ch3_valid"] is False
    assert row["ch3_hold"] is True
