"""Builders for synthetic models, shaped like the bench unit's data."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fujilib.devices.models import (
    AnalyzerStatus,
    ChannelStatus,
    DisplayState,
    Frame,
    Reading,
    ReadingState,
    TransferTiming,
)
from fujilib.protocol.base import ProtocolKind
from fujilib.registry.channels import ChannelId, ChannelRole, Gas, LabelSource
from fujilib.registry.enums import AlarmState, DisplayScreen, ErrorCode, ManualCalibrationStep
from fujilib.registry.units import Unit
from fujilib.testing import BENCH_BANK_PATH

#: A fixed wall-clock origin for synthetic timings.
T0 = datetime(2026, 9, 28, 16, 0, 0, tzinfo=UTC)
#: A fixed monotonic origin (nanoseconds).
MONO0 = 5_000_000_000


def timing(offset_ms: float = 0.0, latency_ms: float = 20.0) -> TransferTiming:
    start = T0 + timedelta(milliseconds=offset_ms)
    mono = MONO0 + int(offset_ms * 1e6)
    return TransferTiming(
        requested_at=start,
        received_at=start + timedelta(milliseconds=latency_ms),
        t_request_mono_ns=mono,
        t_reply_mono_ns=mono + int(latency_ms * 1e6),
    )


def status(
    *,
    rng: int = 1,
    hold: bool = False,
    zero: bool = False,
    span: bool = False,
    errors: tuple[ErrorCode, ...] = (),
) -> ChannelStatus:
    return ChannelStatus(
        range=rng,
        zero_calibrating=zero,
        span_calibrating=span,
        auto_zero_running=False,
        auto_span_running=False,
        hold=hold,
        errors=frozenset(errors),
    )


def reading(
    channel: ChannelId,
    gas: Gas,
    raw: int,
    decimals: int,
    *,
    unit: Unit = Unit.VOL_PERCENT,
    state: ReadingState = ReadingState.OK,
    channel_status: ChannelStatus | None = None,
    label_source: LabelSource = LabelSource.ASSERTED,
    role: ChannelRole = ChannelRole.INSTANTANEOUS,
) -> Reading:
    return Reading(
        channel=channel,
        gas=gas,
        suggested_gas=gas,
        label_source=label_source,
        role=role,
        value=raw / 10**decimals,
        unit=unit,
        raw_value=raw,
        decimals=decimals,
        status=channel_status if channel_status is not None else status(),
        state=state,
        protocol=ProtocolKind.MODBUS_RTU,
    )


def analyzer(
    *,
    instrument_error: bool = False,
    errors: tuple[ErrorCode, ...] = (),
    alarms: tuple[AlarmState | int, ...] = (AlarmState.NONE,) * 6,
    auto_calibration: bool = False,
) -> AnalyzerStatus:
    return AnalyzerStatus(
        instrument_error=instrument_error,
        calibration_error=False,
        errors=frozenset(errors),
        alarms=alarms,
        peak_count=0,
        peak_alarm=False,
        auto_calibration_running=auto_calibration,
        display=DisplayState(
            screen=DisplayScreen.MEASUREMENT,
            calibration_step=ManualCalibrationStep.NONE,
            top_channel=ChannelId.CH1,
            cursor_channel=None,
        ),
    )


def bench_readings() -> tuple[Reading, ...]:
    """CO2, CO and O2 as the bench unit reported them at capture."""
    return (
        reading(ChannelId.CH1, Gas.CO2, -11, 2),
        reading(ChannelId.CH2, Gas.CO, -9, 3),
        reading(ChannelId.CH3, Gas.O2, 2029, 2),
    )


def frame(
    readings: tuple[Reading, ...] | None = None,
    status_block: AnalyzerStatus | None = None,
    *,
    detail: bool = True,
) -> Frame:
    return Frame(
        readings=readings if readings is not None else bench_readings(),
        analyzer=(status_block or analyzer()) if detail else None,
        protocol=ProtocolKind.MODBUS_RTU,
        readings_timing=timing(0.0),
        status_timing=timing(25.0) if detail else None,
        raw=b"\x00" * 242,
    )


def bench_banks() -> tuple[dict[int, int], dict[int, int]]:
    """``(holding, input)`` banks of the committed, sanitized bench capture."""
    data = json.loads(BENCH_BANK_PATH.read_text(encoding="utf-8"))
    return (
        {int(a, 16): w for a, w in data["holding"].items()},
        {int(a, 16): w for a, w in data["input"].items()},
    )
