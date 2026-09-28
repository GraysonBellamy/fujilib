"""Pure decoders: register banks to models (design §2.5-§2.9, §8).

A **bank** maps addresses of one register table to the words read there
(``BlockRead.to_bank`` builds one from a reply; a register dump is one). Every
function here is pure: no I/O, no clock reads, no caches. The session supplies
what it has cached (ranges, labels, the channels established so far) and the
timing of each transaction.

Decoding is total where the analyzer may legitimately surprise: an
undocumented enum value is kept as an ``int``, and a concentration whose
decimal point does not decode becomes a reading with ``value=None`` and state
``unknown`` instead of failing the poll. Only structural problems, such as a
missing word, raise :class:`~fujilib.errors.FujiDecodeError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.devices.models import (
    AdcValues,
    AnalyzerMetadata,
    AnalyzerStatus,
    AutoCalibrationSchedule,
    AutoZeroSchedule,
    AveragePeriod,
    CalibrationLogEntry,
    CalibrationScope,
    ChannelInfo,
    ChannelStatus,
    DisplayState,
    ErrorLogEntry,
    Frame,
    PartialTimestamp,
    RangeInfo,
    Reading,
    ReadingState,
    Schedule,
)
from fujilib.errors import ErrorContext, FujiDecodeError
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.codec import (
    MAX_DECIMALS,
    decode_bcd,
    decode_bool,
    decode_chars,
    decode_enum,
    decode_int,
    decode_raw,
    decode_uint32_lh,
    scale,
)
from fujilib.registry.channels import (
    CHANNELS,
    MEASURED_CHANNELS,
    ChannelId,
    ChannelRole,
    Gas,
    LabelSource,
)
from fujilib.registry.enums import (
    AlarmState,
    CalibrationKind,
    CalibrationRangeMode,
    DayOfWeek,
    DisplayScreen,
    ErrorCode,
    ErrorScope,
    HoldMode,
    ManualCalibrationStep,
    PeriodUnit,
    RangeIndex,
    ScheduleCycleUnit,
    ZeroCalibrationMode,
)
from fujilib.registry.registers import (
    CALIBRATION_LOG,
    ERROR_LOG,
    REGISTRY,
    RegisterSpec,
    ScalingKind,
)
from fujilib.registry.typecode import TypeCode, decode_type_code, suggest_labels
from fujilib.registry.units import Unit, unit_from_code

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from enum import IntEnum

    from fujilib.devices.models import TransferTiming

__all__ = [
    "Bank",
    "RegisterValue",
    "decode_adc",
    "decode_analyzer_status",
    "decode_calibration_log",
    "decode_channel_status",
    "decode_clock",
    "decode_current_ranges",
    "decode_error_log",
    "decode_frame",
    "decode_identity",
    "decode_metadata",
    "decode_ranges",
    "decode_register",
    "derive_states",
    "label_channels",
    "nonzero_channels",
    "range_scaling",
    "words_of",
]

#: Addresses of one register table and the words read there.
type Bank = Mapping[int, int]

_EMPTY: Final = 0xFFFF
_EMPTY_BYTE: Final = 0x00FF


def words_of(bank: Bank, address: int, count: int = 1) -> tuple[int, ...]:
    """The ``count`` words from ``address``.

    Raises:
        FujiDecodeError: a word is missing from ``bank``.
    """
    try:
        return tuple(bank[a] for a in range(address, address + count))
    except KeyError as exc:
        msg = f"register 0x{exc.args[0]:04X} was not read"
        raise FujiDecodeError(msg, context=ErrorContext(register_address=exc.args[0])) from None


def _spec_words(bank: Bank, name: str) -> tuple[int, ...]:
    spec = REGISTRY.resolve(name)
    return words_of(bank, spec.address, spec.count)


def _word(bank: Bank, name: str) -> int:
    return _spec_words(bank, name)[0]


def _flag(bank: Bank, name: str) -> bool:
    return decode_bool(_word(bank, name))


def _enum[E: IntEnum](bank: Bank, name: str, enum: type[E]) -> E | int:
    return decode_enum(enum, _word(bank, name))


def _range_number(bank: Bank, name: str) -> int:
    """A range register (0 = range 1) as a 1-based range number."""
    rng = _enum(bank, name, RangeIndex)
    return rng.number if isinstance(rng, RangeIndex) else rng + 1


def _channel_minus_one(raw: int) -> ChannelId | None:
    return ChannelId.from_number(raw + 1) if 0 <= raw < len(CHANNELS) else None


# --- Identity, ranges and presence -----------------------------------------------------


def decode_identity(bank: Bank) -> tuple[TypeCode, str]:
    """The type code (with digits 27-29 when they were read) and the serial number.

    Raises:
        FujiDecodeError: the identity registers were not read, or are not characters.
    """
    raw = decode_chars(_spec_words(bank, "identity.type_code"), strip=False)
    ext = REGISTRY.resolve("identity.type_code_ext")
    if all(a in bank for a in range(ext.address, ext.last_address + 1)):
        raw += decode_chars(words_of(bank, ext.address, ext.count), strip=False)
    serial = decode_chars(_spec_words(bank, "identity.serial_number"))
    return decode_type_code(raw), serial


def decode_ranges(bank: Bank) -> tuple[RangeInfo, ...]:
    """The range tables of channels 1-5.

    A range whose decimal point does not decode has a ``nan`` full scale.

    Raises:
        FujiDecodeError: the range registers were not read.
    """
    out: list[RangeInfo] = []
    for channel in MEASURED_CHANNELS:
        c = channel.number
        units: list[Unit] = []
        full_scale: list[float] = []
        decimals: list[int] = []
        for r in (1, 2):
            dp = _word(bank, f"range.ch{c}.range{r}.decimals")
            raw = _word(bank, f"range.ch{c}.range{r}.full_scale")
            units.append(unit_from_code(_word(bank, f"range.ch{c}.range{r}.unit")))
            full_scale.append(scale(raw, dp) if 0 <= dp <= MAX_DECIMALS else float("nan"))
            decimals.append(dp)
        out.append(
            RangeInfo(
                channel=channel,
                count=_word(bank, f"range.ch{c}.count"),
                units=tuple(units),
                full_scale=tuple(full_scale),
                decimals=tuple(decimals),
            )
        )
    return tuple(out)


def decode_current_ranges(bank: Bank) -> Mapping[ChannelId, int]:
    """The range each of channels 1-5 is measuring on, 1 or 2 (input 30038-30042).

    Raises:
        FujiDecodeError: the current-range registers were not read.
    """
    return MappingProxyType(
        {c: _range_number(bank, f"range.ch{c.number}.current") for c in MEASURED_CHANNELS}
    )


def _triple(bank: Bank, channel: ChannelId) -> tuple[int, int, int]:
    n = channel.number
    value = decode_int(_word(bank, f"reading.ch{n}.value"), signed=True)
    return value, _word(bank, f"reading.ch{n}.decimals"), _word(bank, f"reading.ch{n}.unit")


def nonzero_channels(bank: Bank) -> frozenset[ChannelId]:
    """Channels whose reading triple is not all zero in ``bank``.

    An all-zero triple is a legal zero reading (0, dp 0, vol%), not proof of
    absence, so this only ever *adds* channels (design §2.9, step 2).
    """
    return frozenset(c for c in CHANNELS if any(_triple(bank, c)))


def label_channels(
    present: Iterable[ChannelId],
    *,
    asserted: Mapping[ChannelId, Gas] | None = None,
    type_code: TypeCode | None = None,
) -> tuple[ChannelInfo, ...]:
    """Label the established channels (design §2.9).

    The established channels are ``present`` plus every asserted one. An
    asserted label wins and is the only source fit for calculation; otherwise
    ``gas`` is ``UNKNOWN`` and the type code (or the layout rule) only
    *suggests* one.
    """
    asserted = asserted or {}
    channels = sorted(set(present) | set(asserted), key=lambda c: c.number)
    suggestions = suggest_labels(type_code, channels)
    out: list[ChannelInfo] = []
    for channel in channels:
        suggestion = suggestions.get(channel)
        role = suggestion.role if suggestion is not None else ChannelRole.UNKNOWN
        gas = asserted.get(channel)
        if gas is not None and suggestion is None and channel.is_measured:
            role = ChannelRole.INSTANTANEOUS
        out.append(
            ChannelInfo(
                channel=channel,
                gas=gas if gas is not None else Gas.UNKNOWN,
                suggested_gas=suggestion.gas if suggestion is not None else None,
                role=role,
                label_source=(
                    LabelSource.ASSERTED
                    if gas is not None
                    else (suggestion.source if suggestion is not None else LabelSource.UNKNOWN)
                ),
                derived_from=suggestion.derived_from if suggestion is not None else None,
            )
        )
    return tuple(out)


# --- Status and validity ------------------------------------------------------------------


def decode_channel_status(bank: Bank, channel: ChannelId) -> ChannelStatus:
    """The status of measured ``channel`` (1-5), from both poll blocks.

    Raises:
        FujiDecodeError: ``channel`` is not measured, or a status word was not read.
    """
    if not channel.is_measured:
        msg = f"{channel.value} has no status registers"
        raise FujiDecodeError(msg, context=ErrorContext(channel=channel.value))
    c = channel.number
    errors = frozenset(
        ErrorCode(e) for e in range(4, 10) if _flag(bank, f"error.ch{c}.e{e}.active")
    )
    return ChannelStatus(
        range=_range_number(bank, f"range.ch{c}.current"),
        zero_calibrating=_flag(bank, f"status.ch{c}.zero_calibrating"),
        span_calibrating=_flag(bank, f"status.ch{c}.span_calibrating"),
        auto_zero_running=_flag(bank, f"status.ch{c}.auto_zero_running"),
        auto_span_running=_flag(bank, f"status.ch{c}.auto_span_running"),
        hold=_flag(bank, f"status.ch{c}.hold"),
        errors=errors,
    )


def decode_analyzer_status(bank: Bank) -> AnalyzerStatus:
    """The analyzer-level status, from both poll blocks.

    Raises:
        FujiDecodeError: a status word was not read.
    """
    errors = frozenset(ErrorCode(e) for e in (1, 2, 3, 10) if _flag(bank, f"error.e{e}.active"))
    alarms = tuple(_enum(bank, f"alarm{n}.state", AlarmState) for n in range(1, 7))
    return AnalyzerStatus(
        instrument_error=_flag(bank, "status.instrument_error"),
        calibration_error=_flag(bank, "status.calibration_error"),
        errors=errors,
        alarms=alarms,
        peak_count=_word(bank, "peak_alarm.count_per_hour"),
        peak_alarm=_flag(bank, "peak_alarm.active"),
        auto_calibration_running=_flag(bank, "status.auto_calibration_running"),
        display=DisplayState(
            screen=_enum(bank, "display.screen", DisplayScreen),
            calibration_step=_enum(bank, "display.calibration_step", ManualCalibrationStep),
            top_channel=_channel_minus_one(_word(bank, "display.top_channel")),
            cursor_channel=_channel_minus_one(_word(bank, "display.cursor_channel")),
        ),
    )


def _own_state(status: ChannelStatus | None, analyzer: AnalyzerStatus) -> ReadingState:
    if analyzer.instrument_error or analyzer.errors:
        return ReadingState.ANALYZER_ERROR
    if status is not None and status.errors:
        return ReadingState.CHANNEL_ERROR
    if status is not None and status.calibrating:
        return ReadingState.CALIBRATING
    if analyzer.auto_calibration_running:
        return ReadingState.AUTO_CALIBRATION
    if status is not None and status.hold:
        return ReadingState.HOLD
    return ReadingState.OK


_DERIVED_ROLES: Final = frozenset(
    {ChannelRole.O2_CORRECTED, ChannelRole.O2_CORRECTED_AVERAGE, ChannelRole.O2_AVERAGE}
)


def _is_derived(info: ChannelInfo) -> bool:
    if info.role in _DERIVED_ROLES:
        return True
    return info.role is ChannelRole.UNKNOWN and not info.channel.is_measured


def derive_states(
    channels: Sequence[ChannelInfo],
    statuses: Mapping[ChannelId, ChannelStatus],
    analyzer: AnalyzerStatus | None,
    *,
    undecodable: Iterable[ChannelId] = (),
) -> Mapping[ChannelId, ReadingState]:
    """Apply the validity rule of design §8 to every established channel.

    - Without the status block every state is ``UNKNOWN``.
    - A channel whose value did not decode is ``UNKNOWN``.
    - A channel is invalid when the analyzer reports an instrument error, the
      channel reports an error 4-9, it is calibrating, an auto calibration is
      running, or it is held.
    - A derived channel (O2-corrected, an average, or an unlabelled channel
      above 5) is also ``SOURCE_INVALID`` when its source channel or the O2
      channel is invalid. When its source is not known, any invalid measured
      channel makes it invalid.
    """
    if analyzer is None:
        return MappingProxyType({c.channel: ReadingState.UNKNOWN for c in channels})
    states = {c.channel: _own_state(statuses.get(c.channel), analyzer) for c in channels}
    o2 = next(
        (
            c.channel
            for c in channels
            if Gas.O2 in {c.gas, c.suggested_gas} and c.role is ChannelRole.INSTANTANEOUS
        ),
        None,
    )
    measured_invalid = any(
        states[c.channel] is not ReadingState.OK for c in channels if not _is_derived(c)
    )
    for info in channels:
        if not _is_derived(info) or states[info.channel] is not ReadingState.OK:
            continue
        if info.derived_from is None and info.role is not ChannelRole.O2_AVERAGE:
            invalid = measured_invalid
        else:
            sources = {s for s in (info.derived_from, o2) if s is not None and s in states}
            invalid = any(states[s] is not ReadingState.OK for s in sources)
        if invalid:
            states[info.channel] = ReadingState.SOURCE_INVALID
    for channel in states.keys() & set(undecodable):
        states[channel] = ReadingState.UNKNOWN
    return MappingProxyType(states)


def decode_frame(
    bank: Bank,
    channels: Sequence[ChannelInfo],
    *,
    readings_timing: TransferTiming,
    status_timing: TransferTiming | None = None,
    raw: bytes = b"",
    protocol: ProtocolKind = ProtocolKind.MODBUS_RTU,
) -> Frame:
    """Decode a poll of the established ``channels``.

    With ``status_timing`` the bank must hold both poll blocks and every
    reading gets a state; without it only the readings block was read, and
    every state is ``UNKNOWN`` (``poll(detail=False)``).

    Raises:
        FujiDecodeError: a needed word was not read.
    """
    detail = status_timing is not None
    analyzer = decode_analyzer_status(bank) if detail else None
    statuses = (
        {
            c.channel: decode_channel_status(bank, c.channel)
            for c in channels
            if c.channel.is_measured
        }
        if detail
        else {}
    )
    decoded: dict[ChannelId, tuple[float | None, int, int, Unit]] = {}
    for info in channels:
        value, dp, unit_code = _triple(bank, info.channel)
        scaled = scale(value, dp) if 0 <= dp <= MAX_DECIMALS else None
        decoded[info.channel] = (scaled, value, dp, unit_from_code(unit_code))
    undecodable = [c for c, (v, *_rest) in decoded.items() if v is None]
    states = derive_states(channels, statuses, analyzer, undecodable=undecodable)
    readings = tuple(
        Reading(
            channel=info.channel,
            gas=info.gas,
            suggested_gas=info.suggested_gas,
            label_source=info.label_source,
            role=info.role,
            value=decoded[info.channel][0],
            unit=decoded[info.channel][3],
            raw_value=decoded[info.channel][1],
            decimals=decoded[info.channel][2],
            status=statuses.get(info.channel),
            state=states[info.channel],
            protocol=protocol,
        )
        for info in channels
    )
    return Frame(
        readings=readings,
        analyzer=analyzer,
        protocol=protocol,
        readings_timing=readings_timing,
        status_timing=status_timing,
        raw=raw,
    )


# --- Logs -------------------------------------------------------------------------------


def decode_error_log(bank: Bank) -> tuple[ErrorLogEntry, ...]:
    """The error log, newest first, without empty entries.

    The log has no year and no month; each time is a :class:`PartialTimestamp`.

    Raises:
        FujiDecodeError: a log word was not read.
    """
    out: list[ErrorLogEntry] = []
    for index in range(ERROR_LOG.records):
        words = words_of(bank, ERROR_LOG.record_address(1, index), ERROR_LOG.record_words)
        number = decode_int(words[0], signed=True)
        if number < 0:
            continue
        code = decode_enum(ErrorCode, number + 1)
        channel = None
        if isinstance(code, ErrorCode) and code.scope is ErrorScope.CHANNEL:
            channel = _channel_minus_one(words[4])
        out.append(
            ErrorLogEntry(code, channel, PartialTimestamp(None, words[1], words[2], words[3]))
        )
    return tuple(out)


def decode_calibration_log(bank: Bank, channel: ChannelId) -> tuple[CalibrationLogEntry, ...]:
    """One channel's calibration log, newest first, without empty records.

    A record is empty when its channel field reads -1 (``FFFFh``, or ``00FFh``:
    the manual writes "FF(-1)").

    Raises:
        FujiDecodeError: ``channel`` is not measured, or a log word was not read.
    """
    if not channel.is_measured:
        msg = f"{channel.value} has no calibration log"
        raise FujiDecodeError(msg, context=ErrorContext(channel=channel.value))
    out: list[CalibrationLogEntry] = []
    fields = {f.name: f for f in CALIBRATION_LOG.fields}
    for index in range(CALIBRATION_LOG.records):
        base = CALIBRATION_LOG.record_address(channel.number, index)
        words = words_of(bank, base, CALIBRATION_LOG.record_words)
        if words[0] in {_EMPTY, _EMPTY_BYTE}:
            continue
        kind = decode_enum(CalibrationKind, words[fields["kind"].offset])
        count_at = fields["detector_count"].offset
        deviation = decode_int(words[fields["deviation"].offset], signed=True)
        out.append(
            CalibrationLogEntry(
                channel=channel,
                range=kind.range if isinstance(kind, CalibrationKind) else kind // 2 + 1,
                kind=kind,
                detector_count=decode_uint32_lh(words[count_at : count_at + 2]),
                deviation_percent_fs=scale(deviation, 1),
                at=PartialTimestamp(
                    *(words[fields[f].offset] for f in ("month", "day", "hour", "minute"))
                ),
            )
        )
    return tuple(out)


# --- Clock and A/D -------------------------------------------------------------------------


def decode_clock(words: Sequence[int]) -> datetime:
    """The analyzer's clock: seven BCD words, year (two digits) to second.

    The result is naive local time. The weekday register must agree with the
    date (0 = Sunday).

    Raises:
        FujiDecodeError: the words are not seven BCD values forming a valid date.
    """
    if len(words) != 7:  # noqa: PLR2004
        msg = f"the clock is 7 words, got {len(words)}"
        raise FujiDecodeError(msg)
    year, month, day, weekday, hour, minute, second = (decode_bcd(w) for w in words)
    try:
        at = datetime(2000 + year, month, day, hour, minute, second)
    except ValueError as exc:
        msg = f"the clock does not read as a date: {exc}"
        raise FujiDecodeError(msg, context=ErrorContext(extra={"words": tuple(words)})) from exc
    if (at.weekday() + 1) % 7 != weekday:
        msg = f"the clock's weekday {weekday} does not match {at.date()}"
        raise FujiDecodeError(msg, context=ErrorContext(extra={"words": tuple(words)}))
    return at


def decode_adc(words: Sequence[int], *, received_at: datetime, t_mono_ns: int) -> AdcValues:
    """The 21 A/D counts (42 words, long words low word first), grouped in table order.

    Raises:
        FujiDecodeError: not exactly 42 words.
    """
    if len(words) != 42:  # noqa: PLR2004
        msg = f"the A/D block is 42 words, got {len(words)}"
        raise FujiDecodeError(msg)
    counts = tuple(decode_uint32_lh(words[i : i + 2]) for i in range(0, 42, 2))
    return AdcValues(
        inputs=counts[0:5],
        temperatures=counts[5:10],
        resistances=counts[10:14] + counts[17:21],
        pressure=counts[14],
        reference_voltage=counts[15],
        ground=counts[16],
        raw=counts,
        received_at=received_at,
        t_mono_ns=t_mono_ns,
    )


# --- Settings -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisterValue:
    """One register decoded against its spec, for display and parameter reads."""

    spec: RegisterSpec
    words: tuple[int, ...]
    raw: int | bool | str
    """The value the codec decodes, before scaling or enum lookup."""
    value: float | int | bool | str | None
    """Scaled, or an enum member; ``None`` when a scaled value cannot be scaled."""
    unit: str | None


def range_scaling(
    spec: RegisterSpec,
    ranges: Sequence[RangeInfo],
    *,
    alarm_targets: Mapping[int, ChannelId] | None = None,
) -> tuple[int, Unit] | None:
    """The decimals and unit that scale ``spec``, or ``None`` if they cannot be known.

    ``BY_RANGE`` uses the spec's own channel and range. ``BY_ALARM_TARGET``
    needs the alarm's target channel, which only ``alarm_targets`` can supply
    because the target register's encoding is contested.
    """
    channel: ChannelId | None = spec.channel
    if spec.scaling.kind is ScalingKind.BY_ALARM_TARGET:
        channel = (alarm_targets or {}).get(spec.alarm or 0)
    elif spec.scaling.kind is not ScalingKind.BY_RANGE:
        return None
    if channel is None or spec.range is None:
        return None
    for info in ranges:
        if info.channel is channel and spec.range <= len(info.units):
            unit, _full_scale, decimals = info.of(spec.range)
            return decimals, unit
    return None


def decode_register(
    spec: RegisterSpec,
    words: Sequence[int],
    *,
    scaling: tuple[int, Unit] | None = None,
) -> RegisterValue:
    """Decode ``spec`` from its ``words``.

    ``scaling`` gives the decimals and unit of a range-scaled register; an
    inline-scaled concentration is decoded with :func:`decode_frame` instead
    and comes back raw here.

    Raises:
        FujiDecodeError: the words do not decode as the spec's type.
    """
    raw = decode_raw(words, spec.dtype)
    value: float | int | bool | str | None = raw
    unit = spec.unit
    if spec.enum is not None and isinstance(raw, int) and not isinstance(raw, bool):
        value = decode_enum(spec.enum, raw)
    kind = spec.scaling.kind
    if kind in {ScalingKind.BY_RANGE, ScalingKind.BY_ALARM_TARGET}:
        if scaling is None or not 0 <= scaling[0] <= MAX_DECIMALS or not isinstance(raw, int):
            value = None
        else:
            value = scale(raw, scaling[0])
            unit = scaling[1].value
    elif kind is ScalingKind.FIXED and isinstance(raw, int) and spec.scaling.decimals is not None:
        value = scale(raw, spec.scaling.decimals)
    return RegisterValue(spec=spec, words=tuple(words), raw=raw, value=value, unit=unit)


def _schedule(bank: Bank, prefix: str) -> Schedule:
    return Schedule(
        enabled=_flag(bank, f"{prefix}.enabled"),
        start_day=_enum(bank, f"{prefix}.start_day", DayOfWeek),
        start_hour_raw=_word(bank, f"{prefix}.start_hour"),
        start_minute_raw=_word(bank, f"{prefix}.start_minute"),
        cycle=_word(bank, f"{prefix}.cycle"),
        cycle_unit=_enum(bank, f"{prefix}.cycle_unit", ScheduleCycleUnit),
    )


def _response_times(
    bank: Bank, channels: Sequence[ChannelInfo]
) -> tuple[tuple[int, ...], int, Mapping[ChannelId, int]]:
    ndir = tuple(_word(bank, f"response_time.ndir{k}") for k in range(1, 5))
    o2 = _word(bank, "response_time.o2")
    by_channel: dict[ChannelId, int] = {}
    slot = 0
    for info in channels:
        gas = info.gas if info.gas is not Gas.UNKNOWN else info.suggested_gas
        if gas is None or gas is Gas.UNKNOWN or info.role is not ChannelRole.INSTANTANEOUS:
            continue
        if gas is Gas.O2:
            by_channel[info.channel] = o2
        elif slot < len(ndir):
            by_channel[info.channel] = ndir[slot]
            slot += 1
    return ndir, o2, MappingProxyType(by_channel)


def decode_metadata(
    bank: Bank,
    *,
    serial_number: str,
    ranges: Sequence[RangeInfo],
    channels: Sequence[ChannelInfo],
    current_range: Mapping[ChannelId, int],
    captured_at: datetime,
    clock: datetime | None = None,
    clock_read_at: datetime | None = None,
) -> AnalyzerMetadata:
    """The settings snapshot, from the holding bank and what the session has cached.

    Response times are mapped onto channels only where the labels say which
    channel is O2 and which are NDIR components (in channel order).

    Raises:
        FujiDecodeError: a holding word was not read.
    """
    ndir, o2, by_channel = _response_times(bank, channels)
    gases: dict[tuple[ChannelId, int], tuple[float | None, float | None]] = {}
    for info in ranges:
        for rng in range(1, info.count + 1):
            pair: list[float | None] = []
            for kind in ("zero", "span"):
                spec = REGISTRY.resolve(
                    f"calibration_gas.ch{info.channel.number}.range{rng}.{kind}"
                )
                decoded = decode_register(
                    spec, words_of(bank, spec.address), scaling=range_scaling(spec, ranges)
                )
                pair.append(decoded.value if isinstance(decoded.value, float) else None)
            gases[info.channel, rng] = (pair[0], pair[1])
    return AnalyzerMetadata(
        serial_number=serial_number,
        ranges=tuple(ranges),
        current_range=MappingProxyType(dict(current_range)),
        response_time_s=by_channel,
        response_time_ndir_s=ndir,
        response_time_o2_s=o2,
        moving_average=tuple(
            AveragePeriod(
                period=_word(bank, f"moving_average{k}.period"),
                unit=_enum(bank, f"moving_average{k}.unit", PeriodUnit),
            )
            for k in range(1, 5)
        ),
        calibration_gas=MappingProxyType(gases),
        calibration_scope=MappingProxyType(
            {
                c: CalibrationScope(
                    zero_mode=_enum(
                        bank, f"calibration.ch{c.number}.zero_mode", ZeroCalibrationMode
                    ),
                    range_mode=_enum(
                        bank, f"calibration.ch{c.number}.range_mode", CalibrationRangeMode
                    ),
                )
                for c in MEASURED_CHANNELS
            }
        ),
        hold_mode=_enum(bank, "hold.mode", HoldMode),
        output_hold=_flag(bank, "output_hold.enabled"),
        auto_calibration=AutoCalibrationSchedule(
            schedule=_schedule(bank, "auto_calibration"),
            channels=MappingProxyType(
                {
                    c: _flag(bank, f"auto_calibration.ch{c.number}.included")
                    for c in MEASURED_CHANNELS
                }
            ),
            ranges=MappingProxyType(
                {
                    c: _range_number(bank, f"auto_calibration.ch{c.number}.range")
                    for c in MEASURED_CHANNELS
                }
            ),
            flow_times_s=tuple(_word(bank, f"auto_calibration.flow_time{k}") for k in range(1, 8)),
        ),
        auto_zero=AutoZeroSchedule(
            schedule=_schedule(bank, "auto_zero"),
            flow_time_s=_word(bank, "auto_zero.flow_time"),
        ),
        clock=clock,
        clock_read_at=clock_read_at,
        captured_at=captured_at,
    )
