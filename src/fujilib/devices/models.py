"""Frozen data models (design §8).

Every model is a ``@dataclass(frozen=True, slots=True)`` and constructible by
keyword without hardware, so consumers can build them in simulators.

**Validity.** Each :class:`Reading` carries a :class:`ReadingState`, one token
from a closed vocabulary that says whether the value is live and, if not, why.
``Reading.valid`` derives from it: ``True`` for ``ok``, ``None`` when the status
was not read, ``False`` otherwise. Raw values are always kept; validity flags
them, it does not hide them.

**Row columns.** The columns a reading and the analyzer status contribute to a
row are defined once, here (:data:`READING_COLUMNS`, :data:`ANALYZER_COLUMNS`).
:meth:`Reading.as_dict`, :meth:`Frame.as_long_rows` and
:func:`fujilib.sinks.base.sample_to_row` all use these definitions, so they
cannot disagree. Every column value is a scalar (``float``, ``int``, ``str``,
``bool`` or ``None``); sets and tuples are encoded as strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Final

from fujilib.errors import ErrorContext, FujiValidationError
from fujilib.protocol.modbus.codec import as_decimal
from fujilib.registry.channels import ChannelId, ChannelRole, Gas, LabelSource, coerce_channel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from decimal import Decimal

    from fujilib.devices.capability import Availability, Capability
    from fujilib.protocol.base import ProtocolKind
    from fujilib.registry.enums import (
        AlarmState,
        CalibrationKind,
        CalibrationRangeMode,
        DayOfWeek,
        DisplayScreen,
        ErrorCode,
        HoldMode,
        ManualCalibrationStep,
        PeriodUnit,
        ScheduleCycleUnit,
        ZeroCalibrationMode,
    )
    from fujilib.registry.typecode import TypeCode
    from fujilib.registry.units import Unit
    from fujilib.transport.base import SerialSettings

__all__ = [
    "ANALYZER_COLUMNS",
    "READING_COLUMNS",
    "AdcValues",
    "AnalyzerMetadata",
    "AnalyzerStatus",
    "AutoCalibrationSchedule",
    "AutoZeroSchedule",
    "AveragePeriod",
    "CalibrationLogEntry",
    "CalibrationScope",
    "ChannelInfo",
    "ChannelStatus",
    "ColumnDef",
    "DeviceHealth",
    "DeviceInfo",
    "DisplayState",
    "ErrorLogEntry",
    "Frame",
    "PartialTimestamp",
    "RangeInfo",
    "Reading",
    "ReadingState",
    "Scalar",
    "Schedule",
    "TransferTiming",
    "encode_codes",
    "encode_enum",
]

#: A value a row column may hold.
type Scalar = float | int | str | bool | None


class ReadingState(StrEnum):
    """Whether a reading is live and, if not, the most important reason why.

    When several reasons apply, the first in this order wins: analyzer error,
    channel error, calibrating, auto calibration, hold, source invalid.
    """

    OK = "ok"
    """Live: no hold, calibration or error."""
    ANALYZER_ERROR = "analyzer_error"
    """The analyzer reports error 1, 2, 3 or 10."""
    CHANNEL_ERROR = "channel_error"
    """The channel reports an error 4-9."""
    CALIBRATING = "calibrating"
    """The channel is being zero- or span-calibrated."""
    AUTO_CALIBRATION = "auto_calibration"
    """An auto calibration or auto zero calibration is running."""
    HOLD = "hold"
    """The channel's output is held; the value is frozen, not live."""
    SOURCE_INVALID = "source_invalid"
    """A derived channel whose source channel or O2 channel is not valid."""
    UNKNOWN = "unknown"
    """The status was not read, or the value did not decode."""

    @property
    def valid(self) -> bool | None:
        """``True`` for ``OK``, ``None`` for ``UNKNOWN``, ``False`` otherwise."""
        if self is ReadingState.OK:
            return True
        if self is ReadingState.UNKNOWN:
            return None
        return False


class DeviceHealth(StrEnum):
    """How completely ``identify()`` succeeded."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


def encode_enum(value: IntEnum | int | None) -> str | None:
    """Encode an enum value for a row: its lower-case name, or the raw number."""
    if value is None:
        return None
    if isinstance(value, IntEnum):
        return value.name.lower()
    return str(value)


def encode_codes(codes: Iterable[int] | None) -> str | None:
    """Encode a set of error codes for a row: sorted and comma-joined, ``""`` if none."""
    if codes is None:
        return None
    return ",".join(str(int(c)) for c in sorted(codes))


# --- Status ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelStatus:
    """Per-channel status of a measured channel (1-5)."""

    range: int
    """The current range, 1 or 2."""
    zero_calibrating: bool
    span_calibrating: bool
    auto_zero_running: bool
    auto_span_running: bool
    hold: bool
    """The channel's output is held: the value is frozen, not live."""
    errors: frozenset[ErrorCode]
    """Errors 4-9 currently active on this channel."""

    @property
    def calibrating(self) -> bool:
        """Whether any zero or span calibration, manual or automatic, is running."""
        return (
            self.zero_calibrating
            or self.span_calibrating
            or self.auto_zero_running
            or self.auto_span_running
        )


@dataclass(frozen=True, slots=True)
class DisplayState:
    """What the front panel shows (input 30181-30183, 30189)."""

    screen: DisplayScreen | int
    calibration_step: ManualCalibrationStep | int
    top_channel: ChannelId | None
    cursor_channel: ChannelId | None


@dataclass(frozen=True, slots=True)
class AnalyzerStatus:
    """Analyzer-level status from a full poll."""

    instrument_error: bool
    calibration_error: bool
    errors: frozenset[ErrorCode]
    """Analyzer-level errors currently active: 1, 2, 3, 10."""
    alarms: tuple[AlarmState | int, ...]
    """States of alarms 1-6, in order."""
    peak_count: int
    peak_alarm: bool
    auto_calibration_running: bool
    display: DisplayState | None

    def as_dict(self) -> dict[str, Scalar]:
        """The analyzer columns of a row (:data:`ANALYZER_COLUMNS`)."""
        return {c.name: c.extract(self) for c in ANALYZER_COLUMNS}


@dataclass(frozen=True, slots=True)
class TransferTiming:
    """Host timing of one Modbus transaction."""

    requested_at: datetime
    """Wall clock (UTC, tz-aware) when the request had been sent: after the
    inter-frame gap, the write and the drain, so no wait for the line is included."""
    received_at: datetime
    """Wall clock (UTC, tz-aware) when the reply had been read."""
    t_request_mono_ns: int
    t_reply_mono_ns: int

    @property
    def midpoint_mono_ns(self) -> int:
        """Monotonic midpoint of request and reply: the best estimate of the reading's time."""
        return (self.t_request_mono_ns + self.t_reply_mono_ns) // 2

    @property
    def midpoint_utc(self) -> datetime:
        """Wall-clock midpoint of request and reply."""
        return self.requested_at + (self.received_at - self.requested_at) / 2

    @property
    def latency_s(self) -> float:
        """Round-trip time in seconds."""
        return (self.received_at - self.requested_at).total_seconds()


# --- Readings and frames ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """One channel's concentration from a poll, with its label, validity and provenance."""

    channel: ChannelId
    gas: Gas
    """``UNKNOWN`` unless asserted by the caller (or, where so labelled, decoded)."""
    suggested_gas: Gas | None
    label_source: LabelSource
    role: ChannelRole
    value: float | None
    """The scaled value, or ``None`` when the triple does not decode."""
    unit: Unit
    raw_value: int
    """The signed integer from the register."""
    decimals: int
    """The decimal-point register, 0-3, so the exact decimal can be rebuilt."""
    status: ChannelStatus | None
    """``None`` for derived channels and when the status block was not read."""
    state: ReadingState
    protocol: ProtocolKind

    @property
    def valid(self) -> bool | None:
        """Whether the value is live; ``None`` when that is unknown (design §8)."""
        return self.state.valid

    def as_decimal(self) -> Decimal | None:
        """The exact decimal value, or ``None`` when the decimal point does not decode."""
        return None if self.value is None else as_decimal(self.raw_value, self.decimals)

    def as_dict(self) -> dict[str, Scalar]:
        """The per-channel columns of a row, without the ``chN_`` prefix."""
        return {c.name: c.extract(self) for c in READING_COLUMNS}


@dataclass(frozen=True, slots=True)
class Frame:
    """Every established channel and the analyzer status from one poll."""

    readings: tuple[Reading, ...]
    analyzer: AnalyzerStatus | None
    """``None`` when only the readings block was read."""
    protocol: ProtocolKind
    readings_timing: TransferTiming
    """Timing of the block that holds every concentration."""
    status_timing: TransferTiming | None
    raw: bytes
    """The words of every block of the poll, big-endian, concatenated."""

    def channel(self, channel: ChannelId | str) -> Reading:
        """The reading of ``channel``.

        Raises:
            FujiValidationError: the channel is unknown or not in this frame.
        """
        cid = coerce_channel(channel)
        for reading in self.readings:
            if reading.channel is cid:
                return reading
        msg = f"{cid.value} is not an established channel of this frame"
        raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))

    @property
    def channels(self) -> tuple[ChannelId, ...]:
        """The channels in this frame, in order."""
        return tuple(r.channel for r in self.readings)

    def as_long_rows(self, *, device: str, address: int) -> list[dict[str, Scalar]]:
        """One row per channel, with the analyzer status repeated on each.

        For SQL unions with long-format siblings; the recorder's rows are wide.
        """
        analyzer: dict[str, Scalar] = (
            self.analyzer.as_dict()
            if self.analyzer is not None
            else dict.fromkeys(c.name for c in ANALYZER_COLUMNS)
        )
        return [
            {
                "device": device,
                "address": address,
                "protocol": self.protocol.value,
                "channel": r.channel.value,
                **r.as_dict(),
                **analyzer,
            }
            for r in self.readings
        ]


# --- Column definitions --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnDef[T]:
    """A row column: its name, scalar type and how to extract it from a model."""

    name: str
    python_type: type[float] | type[int] | type[str] | type[bool]
    extract: Callable[[T], Scalar]


def _status_field[V](reading: Reading, get: Callable[[ChannelStatus], V]) -> V | None:
    return None if reading.status is None else get(reading.status)


#: Per-channel columns, in row order. In a wide row each is prefixed ``chN_``.
READING_COLUMNS: Final[tuple[ColumnDef[Reading], ...]] = (
    ColumnDef("value", float, lambda r: r.value),
    ColumnDef("raw", int, lambda r: r.raw_value),
    ColumnDef("decimals", int, lambda r: r.decimals),
    ColumnDef("unit", str, lambda r: r.unit.value),
    ColumnDef("gas", str, lambda r: r.gas.value),
    ColumnDef("label_source", str, lambda r: r.label_source.value),
    ColumnDef("state", str, lambda r: r.state.value),
    ColumnDef("valid", bool, lambda r: r.valid),
    ColumnDef("hold", bool, lambda r: _status_field(r, lambda s: s.hold)),
    ColumnDef("calibrating", bool, lambda r: _status_field(r, lambda s: s.calibrating)),
    ColumnDef("errors", str, lambda r: encode_codes(_status_field(r, lambda s: s.errors))),
)

#: The number of alarms the status registers report.
ALARM_COUNT: Final = 6


def _alarm(index: int) -> Callable[[AnalyzerStatus], Scalar]:
    def extract(status: AnalyzerStatus) -> Scalar:
        return encode_enum(status.alarms[index]) if index < len(status.alarms) else None

    return extract


#: Analyzer-level columns, in row order.
ANALYZER_COLUMNS: Final[tuple[ColumnDef[AnalyzerStatus], ...]] = (
    ColumnDef("instrument_error", bool, lambda a: a.instrument_error),
    ColumnDef("calibration_error", bool, lambda a: a.calibration_error),
    ColumnDef("analyzer_errors", str, lambda a: encode_codes(a.errors)),
    *(ColumnDef(f"alarm{n}", str, _alarm(n - 1)) for n in range(1, ALARM_COUNT + 1)),
    ColumnDef("auto_calibration_running", bool, lambda a: a.auto_calibration_running),
)


# --- Identity and ranges -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RangeInfo:
    """A measured channel's ranges, from the fixed-setting registers."""

    channel: ChannelId
    count: int
    """How many ranges the channel has, 1 or 2. Unused channels also report ranges."""
    units: tuple[Unit, ...]
    full_scale: tuple[float, ...]
    decimals: tuple[int, ...]

    def of(self, rng: int) -> tuple[Unit, float, int]:
        """``(unit, full_scale, decimals)`` of range ``rng`` (1 or 2).

        Raises:
            FujiValidationError: ``rng`` is not a range of the channel's table.
        """
        if not 1 <= rng <= len(self.units):
            msg = f"{self.channel.value} has no range {rng}"
            raise FujiValidationError(msg, context=ErrorContext(channel=self.channel.value))
        i = rng - 1
        return self.units[i], self.full_scale[i], self.decimals[i]


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """What is known about one established channel's label."""

    channel: ChannelId
    gas: Gas
    suggested_gas: Gas | None
    role: ChannelRole
    label_source: LabelSource
    derived_from: ChannelId | None = None
    """For an O2-corrected value or average, the channel of the corrected component."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """What ``identify()`` established about an analyzer."""

    model: str
    type_code: TypeCode
    serial_number: str
    """The manual's "board" code; on the bench unit, the serial number."""
    channels: tuple[ChannelInfo, ...]
    ranges: tuple[RangeInfo, ...]
    capabilities: Capability
    availability: Mapping[Capability, Availability]
    protocol: ProtocolKind
    address: int
    serial_settings: SerialSettings
    health: DeviceHealth
    firmware: str | None = None
    """Always ``None``: the program version is shown on the display only, not in a register."""


# --- Settings snapshot -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AveragePeriod:
    """One moving-average period (holding 40085-40092)."""

    period: int
    unit: PeriodUnit | int


@dataclass(frozen=True, slots=True)
class CalibrationScope:
    """How far a calibration started on one channel reaches (design §2.6)."""

    zero_mode: ZeroCalibrationMode | int
    range_mode: CalibrationRangeMode | int


@dataclass(frozen=True, slots=True)
class Schedule:
    """An automatic function's schedule.

    The start hour and minute are kept raw: the manual says BCD, but the bench
    unit contradicts it (design §2.6).
    """

    enabled: bool
    start_day: DayOfWeek | int
    start_hour_raw: int
    start_minute_raw: int
    cycle: int
    cycle_unit: ScheduleCycleUnit | int


@dataclass(frozen=True, slots=True)
class AutoCalibrationSchedule:
    """Auto calibration: the schedule, which channels and ranges, and the gas flow times."""

    schedule: Schedule
    channels: Mapping[ChannelId, bool]
    ranges: Mapping[ChannelId, int]
    flow_times_s: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AutoZeroSchedule:
    """Auto zero calibration: the schedule and the gas flow time."""

    schedule: Schedule
    flow_time_s: int


@dataclass(frozen=True, slots=True)
class AnalyzerMetadata:
    """The read-only settings snapshot a consumer records with its data (design §2.11).

    Response times are per NDIR component slot and a separate O2 slot;
    ``response_time_s`` maps them onto channels only where the channel labels
    say which is which.
    """

    serial_number: str
    ranges: tuple[RangeInfo, ...]
    current_range: Mapping[ChannelId, int]
    response_time_s: Mapping[ChannelId, int]
    response_time_ndir_s: tuple[int, ...]
    response_time_o2_s: int
    moving_average: tuple[AveragePeriod, ...]
    calibration_gas: Mapping[tuple[ChannelId, int], tuple[float | None, float | None]]
    """``(channel, range)`` → ``(zero, span)``, scaled by the range; ``None`` if unscalable."""
    calibration_scope: Mapping[ChannelId, CalibrationScope]
    hold_mode: HoldMode | int
    output_hold: bool
    auto_calibration: AutoCalibrationSchedule
    auto_zero: AutoZeroSchedule
    clock: datetime | None
    """The analyzer's clock: naive local time. ``None`` if unavailable."""
    clock_read_at: datetime | None
    """Host UTC time the clock was read."""
    captured_at: datetime


# --- Diagnostics and logs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdcValues:
    """The undocumented A/D block: raw service counts, not a gas measurement (design §6.6).

    The groups follow the service manual's table order; that interpretation is
    inferred.
    """

    inputs: tuple[int, ...]
    """No. 0-4: the four NDIR inputs and the O2 sensor input."""
    temperatures: tuple[int, ...]
    """No. 5-9."""
    resistances: tuple[int, ...]
    """No. 10-13 and 17-20."""
    pressure: int
    """No. 14."""
    reference_voltage: int
    """No. 15."""
    ground: int
    """No. 16."""
    raw: tuple[int, ...]
    """All 21 counts, in table order."""
    received_at: datetime
    t_mono_ns: int


@dataclass(frozen=True, slots=True)
class PartialTimestamp:
    """A log time without a year (and, in the error log, without a month)."""

    month: int | None
    day: int
    hour: int
    minute: int

    def resolve(self, clock: datetime, *, max_age: timedelta) -> datetime | None:
        """The unique matching time at most ``max_age`` before ``clock``.

        ``clock`` is a reference in the same (naive local) time as the log,
        normally the analyzer's own clock. Returns ``None`` when no candidate
        or more than one candidate falls in the window.
        """
        earliest = clock - max_age
        candidates: list[datetime] = []
        for year in range(earliest.year, clock.year + 1):
            months = [self.month] if self.month is not None else range(1, 13)
            for month in months:
                try:
                    at = datetime(year, month, self.day, self.hour, self.minute)
                except ValueError:
                    continue
                at = at.replace(tzinfo=clock.tzinfo)
                if earliest <= at <= clock:
                    candidates.append(at)
        return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True, slots=True)
class ErrorLogEntry:
    """One error-log entry (newest first in the log)."""

    code: ErrorCode | int
    channel: ChannelId | None
    """``None`` for analyzer-level errors."""
    at: PartialTimestamp


@dataclass(frozen=True, slots=True)
class CalibrationLogEntry:
    """One calibration-log record (firmware 2.24 or later)."""

    channel: ChannelId
    range: int
    kind: CalibrationKind | int
    detector_count: int
    deviation_percent_fs: float
    at: PartialTimestamp
