"""Builders for synthetic readings, statuses and frames (design §10).

For code that consumes fujilib's models without an analyzer or a simulated
line: a downstream adapter's tests, a sink's tests, or an application's own
simulator. Each builder returns the real frozen model, so
:meth:`Sample.from_frame() <fujilib.streaming.sample.Sample.from_frame>` and
:func:`~fujilib.sinks.base.sample_to_row` turn a built :class:`Frame` into
exactly the sample and row a recording of an analyzer produces.

- :func:`reading` and :func:`status` build one channel; :func:`analyzer` the
  analyzer-level status; :func:`frame` a whole poll.
- :func:`timing` builds a transaction's timing from an offset. Its defaults,
  :data:`T0` and :data:`MONO0`, are fixed, so a test's timestamps are too; a
  simulator passes its own clock readings as ``origin`` and ``mono_origin_ns``.
- :func:`bench_readings` is CO2, CO and O2 as the bench ZPA reported them.

Nothing here checks that the parts agree: a ``state`` is taken as given, not
derived from the status. The decoders do that (:mod:`fujilib.devices.decode`);
use :class:`~fujilib.testing.mock.MockAnalyzer` when the decoding itself is
under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

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

__all__ = [
    "MONO0",
    "T0",
    "analyzer",
    "bench_readings",
    "frame",
    "reading",
    "status",
    "timing",
]

#: A fixed wall-clock origin for synthetic timings.
T0: Final = datetime(2026, 9, 28, 16, 0, 0, tzinfo=UTC)
#: A fixed monotonic origin for synthetic timings, in nanoseconds.
MONO0: Final = 5_000_000_000

#: The words of a full poll: 61 of the readings block and 60 of the status block.
_POLL_BYTES: Final = 242


def timing(
    offset_ms: float = 0.0,
    latency_ms: float = 20.0,
    *,
    origin: datetime = T0,
    mono_origin_ns: int = MONO0,
) -> TransferTiming:
    """The timing of a transaction sent ``offset_ms`` after the origin.

    Args:
        offset_ms: When the request was sent, in milliseconds after the origin.
        latency_ms: The round trip, request to reply.
        origin: The wall-clock origin, tz-aware.
        mono_origin_ns: The monotonic origin, the same instant as ``origin``.
    """
    start = origin + timedelta(milliseconds=offset_ms)
    mono = mono_origin_ns + int(offset_ms * 1e6)
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
    """A measured channel's status: live on range 1 unless told otherwise.

    Args:
        rng: The current range, 1 or 2.
        hold: The channel's output is held.
        zero: A manual zero calibration of the channel is under way.
        span: A manual span calibration of the channel is under way.
        errors: The channel's active errors, 4-9.
    """
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
    """One channel's reading, its value ``raw / 10**decimals``.

    Args:
        channel: The channel.
        gas: The gas it carries, also given as the suggested gas.
        raw: The signed integer of the concentration register.
        decimals: The decimal-point register, 0-3.
        unit: The unit of the channel's current range.
        state: The validity state; it is not derived from ``channel_status``.
        channel_status: The channel's status; :func:`status`'s default if omitted.
        label_source: Where the gas label comes from.
        role: The channel's role.
    """
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
    """The analyzer-level status, on the measurement screen with nothing active.

    Args:
        instrument_error: The instrument-error flag.
        errors: The analyzer's active errors: 1, 2, 3 or 10.
        alarms: The states of alarms 1-6, in order.
        auto_calibration: An auto calibration is running.
    """
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
    """CO2, CO and O2 as the bench ZPA reported them at capture."""
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
    readings_timing: TransferTiming | None = None,
    status_timing: TransferTiming | None = None,
) -> Frame:
    """A poll's frame.

    Args:
        readings: The channels; :func:`bench_readings` if omitted.
        status_block: The analyzer status; :func:`analyzer`'s default if omitted.
        detail: ``False`` builds the frame of a poll that read only the
            concentration block: no analyzer status and no status timing.
        readings_timing: The concentration block's timing, which times the
            sample; ``timing(0.0)`` if omitted.
        status_timing: The status block's timing; ``timing(25.0)`` if omitted.
            Ignored without ``detail``.
    """
    if status_timing is None:
        status_timing = timing(25.0)
    return Frame(
        readings=readings if readings is not None else bench_readings(),
        analyzer=(status_block or analyzer()) if detail else None,
        protocol=ProtocolKind.MODBUS_RTU,
        readings_timing=readings_timing if readings_timing is not None else timing(0.0),
        status_timing=status_timing if detail else None,
        raw=b"\x00" * _POLL_BYTES,
    )
