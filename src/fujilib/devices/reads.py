"""Read procedures: a precomputed plan, a client and a pure decoder (design §4.3, §6.6).

Each function reads one of the precomputed plans of
:mod:`fujilib.protocol.modbus.read_plan` through a
:class:`~fujilib.protocol.base.ProtocolClient` and decodes the words with
:mod:`fujilib.devices.decode`. They hold no state: whatever a decoder needs
beyond the words (the established channels, the ranges, the current range)
is passed in by the caller, which caches it (design §6.7). Gates, caches and
availability bookkeeping belong to the session; these functions only read.

A read plan runs under the port's operation lock, so the blocks of one
procedure are never interleaved with other traffic on the port. They are not
simultaneous: the analyzer updates between transactions, and the front panel
stays live (design §1).

**Transactions per procedure** (FC04 unless noted), tested as exact lists:

| Procedure | Transactions |
|---|---|
| `read_frame` | `0000h+61`, `0083h+60`; the first only without detail |
| `read_status` | the same two blocks |
| `read_ranges` | `0425h+35` |
| `read_identity` | `0425h+35`, `0448h+34`, `0000h+36`, then the probes |
| `probe_capabilities` | `03E8h+49` (`03E8h+7` and `03EFh+42` if it fails), `047Ah+3`, `1000h+9` |
| `read_metadata` | FC03 `0000h+64`, `0040h+64`, `0080h+36`; `03E8h+7` with the clock |
| `read_settings` | FC03 `0000h+64`, `0040h+64`, `0080h+44` |
| `read_error_log` | `003Dh+60`, `0079h+10`, then `003Dh+5` to check |
| `read_calibration_log` | five blocks of 63 words and one of 45, then the first record |
| `read_clock` | `03E8h+7` |
| `read_adc` | `03EFh+42` |
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib._deadline import Deadline
from fujilib._logging import get_logger
from fujilib.devices.capability import PROBED_CAPABILITIES, Availability, Capability
from fujilib.devices.decode import (
    decode_adc,
    decode_analyzer_status,
    decode_calibration_log,
    decode_channel_status,
    decode_clock,
    decode_error_log,
    decode_frame,
    decode_identity,
    decode_metadata,
    decode_ranges,
    decode_register,
    nonzero_channels,
    range_scaling,
    words_of,
)
from fujilib.errors import (
    ErrorContext,
    FujiDecodeError,
    FujiModbusIllegalDataAddressError,
    FujiProtocolError,
    FujiValidationError,
)
from fujilib.protocol.modbus.codec import decode_chars, decode_int
from fujilib.protocol.modbus.read_plan import (
    ADC_PLAN,
    CALIBRATION_LOG_PROBE,
    CLOCK_PLAN,
    ERROR_LOG_PLAN,
    IDENTIFY_PLAN,
    METADATA_PLAN,
    POLL_PLAN,
    RANGES_PLAN,
    SERVICE_PLAN,
    SETTINGS_PLAN,
    TYPE_CODE_EXT_PLAN,
    BlockRead,
    calibration_log_plan,
    plan_reads,
)
from fujilib.registry.channels import MEASURED_CHANNELS, ChannelId, coerce_channel
from fujilib.registry.regions import RegisterTable
from fujilib.registry.registers import CALIBRATION_LOG, ERROR_LOG, REGISTRY

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import (
        AdcValues,
        AnalyzerMetadata,
        AnalyzerStatus,
        CalibrationLogEntry,
        ChannelInfo,
        ChannelStatus,
        ErrorLogEntry,
        Frame,
        RangeInfo,
        TransferTiming,
    )
    from fujilib.errors import FujiError
    from fujilib.protocol.base import ProtocolClient
    from fujilib.protocol.modbus.client import PlanReply
    from fujilib.registry.registers import LogSpec, RegisterSpec
    from fujilib.registry.typecode import TypeCode

__all__ = [
    "ClockReading",
    "Identity",
    "ProbeResult",
    "StatusRead",
    "probe_capabilities",
    "probe_capability",
    "read_adc",
    "read_calibration_log",
    "read_clock",
    "read_error_log",
    "read_frame",
    "read_identity",
    "read_metadata",
    "read_ranges",
    "read_registers",
    "read_settings",
    "read_status",
]

_LOG = get_logger("reads")

_CLOCK_WORDS: Final = 7
_EMPTY_RECORD: Final = frozenset({0xFFFF, 0x00FF})


# --- Results ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusRead:
    """The analyzer's status and every measured channel's, from one poll's blocks."""

    analyzer: AnalyzerStatus
    channels: Mapping[ChannelId, ChannelStatus]
    timings: tuple[TransferTiming, ...]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What probing one capability found (design §6.6)."""

    capability: Capability
    availability: Availability
    words: tuple[int, ...] = ()
    """The words read, when the probe read any."""
    error: FujiError | None = None
    """Why the probe did not find the capability supported, when it failed."""
    timing: TransferTiming | None = None


@dataclass(frozen=True, slots=True)
class Identity:
    """What ``identify()`` reads: identity, ranges, presence and capabilities."""

    type_code: TypeCode
    """Digits 1-26, plus 27-29 where the analyzer has them."""
    serial_number: str
    ranges: tuple[RangeInfo, ...]
    nonzero: frozenset[ChannelId]
    """Channels whose reading triple was not all zero (design §2.9 step 2)."""
    probes: Mapping[Capability, ProbeResult]
    timings: tuple[TransferTiming, ...]

    @property
    def availability(self) -> Mapping[Capability, Availability]:
        """What each probe found."""
        return MappingProxyType({c: p.availability for c, p in self.probes.items()})


@dataclass(frozen=True, slots=True)
class ClockReading:
    """The analyzer's clock and when it was read.

    The clock is naive local time with a two-digit year, and it drifts: on the
    bench it ran minutes behind the host. It never replaces host timestamps
    (design §6.6).
    """

    clock: datetime
    timing: TransferTiming

    @property
    def read_at(self) -> datetime:
        """The host's UTC time of the read (the transaction's midpoint)."""
        return self.timing.midpoint_utc


# --- Measurements ----------------------------------------------------------------------------


async def read_frame(
    client: ProtocolClient,
    channels: Sequence[ChannelInfo],
    *,
    detail: bool = True,
    deadline: Deadline | None = None,
) -> Frame:
    """Poll the established ``channels``: two transactions, or one without ``detail``.

    Without ``detail`` only the concentrations are read, and every reading's
    state is ``unknown`` rather than a manufactured "ok" (design §4.3).

    Raises:
        FujiError: a transaction failed. When the status block fails after the
            concentrations were read, the error's ``extra["completed"]`` holds
            the concentration words (design §4.3).
    """
    plan = POLL_PLAN if detail else POLL_PLAN[:1]
    reply = await client.read_plan(plan, deadline=deadline, command="poll")
    timings = reply.timings
    return decode_frame(
        reply.input,
        channels,
        readings_timing=timings[0],
        status_timing=timings[1] if detail else None,
        raw=reply.raw,
    )


async def read_status(client: ProtocolClient, *, deadline: Deadline | None = None) -> StatusRead:
    """The analyzer's status and channels 1-5's, from the two poll blocks."""
    reply = await client.read_plan(POLL_PLAN, deadline=deadline, command="status")
    bank = reply.input
    return StatusRead(
        analyzer=decode_analyzer_status(bank),
        channels=MappingProxyType({c: decode_channel_status(bank, c) for c in MEASURED_CHANNELS}),
        timings=reply.timings,
    )


# --- Identity, ranges and capabilities -----------------------------------------------------


async def read_ranges(
    client: ProtocolClient, *, deadline: Deadline | None = None
) -> tuple[RangeInfo, ...]:
    """The range tables of channels 1-5."""
    reply = await client.read_plan(RANGES_PLAN, deadline=deadline, command="read_ranges")
    return decode_ranges(reply.input)


async def read_identity(
    client: ProtocolClient, *, probe: bool = True, deadline: Deadline | None = None
) -> Identity:
    """Read the type code, serial number, ranges and readings, then probe the capabilities.

    With ``probe=False`` no capability is probed and :attr:`Identity.probes`
    is empty.

    Raises:
        FujiError: a transaction of the identity plan failed. A failed *probe*
            does not raise; it is reported in :attr:`Identity.probes`.
        FujiDecodeError: the type code or serial number is not characters.
    """
    dl = deadline if deadline is not None else Deadline.after(None, operation="identify")
    reply = await client.read_plan(IDENTIFY_PLAN, deadline=dl, command="identify")
    probes: Mapping[Capability, ProbeResult] = MappingProxyType({})
    if probe:
        probes = await probe_capabilities(client, deadline=dl)
    bank = dict(reply.input)
    extension = probes.get(Capability.TYPE_CODE_EXT)
    if extension is not None and extension.availability is Availability.SUPPORTED:
        bank.update(TYPE_CODE_EXT_PLAN[0].to_bank(extension.words))
    type_code, serial = decode_identity(bank)
    timings = reply.timings + tuple(p.timing for p in probes.values() if p.timing is not None)
    return Identity(
        type_code=type_code,
        serial_number=serial,
        ranges=decode_ranges(bank),
        nonzero=nonzero_channels(bank),
        probes=probes,
        timings=tuple(dict.fromkeys(timings)),
    )


async def probe_capabilities(
    client: ProtocolClient,
    capabilities: Iterable[Capability] = PROBED_CAPABILITIES,
    *,
    deadline: Deadline | None = None,
) -> Mapping[Capability, ProbeResult]:
    """Probe each of ``capabilities`` (design §6.6).

    The clock and the A/D values are adjacent, so when both are asked for
    they are probed with one read of both, and separately only if that fails.

    Raises:
        FujiValidationError: a capability is not one that is probed.
        FujiTimeoutError: ``deadline`` expired.
        FujiConnectionError: the port failed.
    """
    wanted = tuple(dict.fromkeys(capabilities))
    for capability in wanted:
        if capability not in PROBED_CAPABILITIES:
            msg = f"{capability!r} is not a probed capability"
            raise FujiValidationError(msg)
    results: dict[Capability, ProbeResult] = {}
    if Capability.CLOCK in wanted and Capability.ADC_VALUES in wanted:
        combined = await _probe_read(client, SERVICE_PLAN[0], deadline)
        if isinstance(combined, tuple):
            words, timing = combined
            clock_words, adc_words = words[:_CLOCK_WORDS], words[_CLOCK_WORDS:]
            results[Capability.CLOCK] = _validate(Capability.CLOCK, clock_words, timing)
            results[Capability.ADC_VALUES] = _validate(Capability.ADC_VALUES, adc_words, timing)
    for capability in wanted:
        if capability not in results:
            results[capability] = await probe_capability(client, capability, deadline=deadline)
    return MappingProxyType({c: results[c] for c in wanted})


_PROBES: Final[Mapping[Capability, BlockRead]] = MappingProxyType(
    {
        Capability.CLOCK: CLOCK_PLAN[0],
        Capability.ADC_VALUES: ADC_PLAN[0],
        Capability.TYPE_CODE_EXT: TYPE_CODE_EXT_PLAN[0],
        Capability.CALIBRATION_LOG: CALIBRATION_LOG_PROBE,
    }
)


async def probe_capability(
    client: ProtocolClient, capability: Capability, *, deadline: Deadline | None = None
) -> ProbeResult:
    """Probe one capability (design §6.6).

    - ``SUPPORTED``: the probe read data that validates.
    - ``UNSUPPORTED``: exception 02. Every probe is a well-formed read inside
      one documented or observed block, so 02 means the block is absent.
    - ``UNKNOWN``: no reply, a damaged reply, or another exception.
    - ``INVALID_DATA``: readable, but the words do not validate.

    Raises:
        FujiValidationError: ``capability`` is not one that is probed.
        FujiTimeoutError: ``deadline`` expired.
        FujiConnectionError: the port failed.
    """
    block = _PROBES.get(capability)
    if block is None:
        msg = f"{capability!r} is not a probed capability"
        raise FujiValidationError(msg)
    outcome = await _probe_read(client, block, deadline)
    if not isinstance(outcome, tuple):
        availability = (
            Availability.UNSUPPORTED
            if isinstance(outcome, FujiModbusIllegalDataAddressError)
            else Availability.UNKNOWN
        )
        return ProbeResult(capability, availability, error=outcome)
    return _validate(capability, *outcome)


async def _probe_read(
    client: ProtocolClient, block: BlockRead, deadline: Deadline | None
) -> tuple[tuple[int, ...], TransferTiming] | FujiProtocolError:
    try:
        reply = await client.read(block, deadline=deadline, command="probe")
    except FujiProtocolError as exc:
        return exc
    return reply.words, reply.timing


def _validate(
    capability: Capability, words: tuple[int, ...], timing: TransferTiming
) -> ProbeResult:
    try:
        if capability is Capability.CLOCK:
            decode_clock(words)
        elif capability is Capability.TYPE_CODE_EXT:
            decode_chars(words)
        elif capability is Capability.CALIBRATION_LOG:
            _check_calibration_record(words)
        # The A/D block has no structure to check beyond its length. Its
        # reference-voltage window defines analyzer error 3 (TN5A1191b p.33),
        # so a count outside it is a real reading, not invalid data.
    except FujiDecodeError as exc:
        return ProbeResult(capability, Availability.INVALID_DATA, words, exc, timing)
    return ProbeResult(capability, Availability.SUPPORTED, words, timing=timing)


def _check_calibration_record(words: Sequence[int]) -> None:
    first = words[0]
    if first in _EMPTY_RECORD:
        return
    channel = decode_int(first, signed=True)
    if not 1 <= channel <= len(MEASURED_CHANNELS):
        msg = f"a calibration-log record's channel field reads {channel}, not -1 or 1-5"
        raise FujiDecodeError(msg, context=ErrorContext(extra={"words": tuple(words)}))


# --- Settings and metadata ------------------------------------------------------------------


async def read_metadata(
    client: ProtocolClient,
    *,
    serial_number: str,
    ranges: Sequence[RangeInfo],
    channels: Sequence[ChannelInfo],
    current_range: Mapping[ChannelId, int],
    clock: bool,
    deadline: Deadline | None = None,
) -> AnalyzerMetadata:
    """The settings snapshot consumers such as capa carry (design §7.2).

    ``clock`` reads the analyzer's clock as well; pass it only when
    :attr:`Capability.CLOCK` is supported. A clock that does not decode is
    reported as ``None``. ``captured_at`` is when the last settings block arrived.

    Raises:
        FujiError: a transaction failed.
    """
    plan = METADATA_PLAN + (CLOCK_PLAN if clock else ())
    reply = await client.read_plan(plan, deadline=deadline, command="read_metadata")
    settings = reply.replies[len(METADATA_PLAN) - 1]
    clock_value: datetime | None = None
    clock_read_at: datetime | None = None
    if clock:
        clock_reply = reply.replies[-1]
        try:
            clock_value = decode_clock(clock_reply.words)
        except FujiDecodeError as exc:
            _LOG.warning("%s: the analyzer clock does not decode: %s", client.label, exc)
        else:
            clock_read_at = clock_reply.timing.midpoint_utc
    return decode_metadata(
        reply.holding,
        serial_number=serial_number,
        ranges=ranges,
        channels=channels,
        current_range=current_range,
        captured_at=settings.timing.received_at,
        clock=clock_value,
        clock_read_at=clock_read_at,
    )


async def read_settings(
    client: ProtocolClient,
    *,
    ranges: Sequence[RangeInfo],
    alarm_targets: Mapping[int, ChannelId] | None = None,
    deadline: Deadline | None = None,
) -> Mapping[str, RegisterValue]:
    """Every holding register, decoded, by name.

    Range-scaled values use ``ranges``; alarm limits need ``alarm_targets``
    (alarm number to channel), because the target register's encoding is
    contested (design §5.2). Without a scale a value is ``None``; its raw word
    is always kept.
    """
    reply = await client.read_plan(SETTINGS_PLAN, deadline=deadline, command="read_settings")
    specs = REGISTRY.in_table(RegisterTable.HOLDING)
    return _decode_specs(specs, reply, ranges, alarm_targets)


async def read_registers(
    client: ProtocolClient,
    names: Iterable[str],
    *,
    ranges: Sequence[RangeInfo] = (),
    alarm_targets: Mapping[int, ChannelId] | None = None,
    deadline: Deadline | None = None,
) -> Mapping[str, RegisterValue]:
    """The registers called ``names``, read in the fewest blocks, decoded, in the order given.

    A concentration (inline scaling) comes back raw; :func:`read_frame` scales it.

    Raises:
        FujiValidationError: a name is not in the registry.
    """
    specs = tuple(dict.fromkeys(REGISTRY.resolve(n) for n in names))
    reply = await client.read_plan(plan_reads(specs), deadline=deadline, command="read_registers")
    return _decode_specs(specs, reply, ranges, alarm_targets)


def _decode_specs(
    specs: Iterable[RegisterSpec],
    reply: PlanReply,
    ranges: Sequence[RangeInfo],
    alarm_targets: Mapping[int, ChannelId] | None,
) -> Mapping[str, RegisterValue]:
    banks = {table: reply.bank(table) for table in RegisterTable}
    return MappingProxyType(
        {
            spec.name: decode_register(
                spec,
                words_of(banks[spec.table], spec.address, spec.count),
                scaling=range_scaling(spec, ranges, alarm_targets=alarm_targets),
            )
            for spec in specs
        }
    )


# --- Logs ------------------------------------------------------------------------------------


async def read_error_log(
    client: ProtocolClient, *, deadline: Deadline | None = None
) -> tuple[ErrorLogEntry, ...]:
    """The error log, newest first (design §4.3).

    A new entry shifts the whole log, so the newest record is read again
    after the scan; if it changed, the log is read once more.
    """
    bank = await _read_log(
        client, ERROR_LOG, 1, ERROR_LOG_PLAN, deadline=deadline, command="read_error_log"
    )
    return decode_error_log(bank)


async def read_calibration_log(
    client: ProtocolClient, channel: ChannelId | str, *, deadline: Deadline | None = None
) -> tuple[CalibrationLogEntry, ...]:
    """One channel's calibration log, newest first; firmware 2.24 or later (design §4.3).

    Raises:
        FujiValidationError: ``channel`` is not one of channels 1-5.
    """
    cid = coerce_channel(channel)
    if not cid.is_measured:
        msg = f"{cid.value} has no calibration log"
        raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
    plan = calibration_log_plan(cid.number)
    bank = await _read_log(
        client, CALIBRATION_LOG, cid.number, plan, deadline=deadline, command="read_calibration_log"
    )
    return decode_calibration_log(bank, cid)


async def _read_log(
    client: ProtocolClient,
    log: LogSpec,
    channel: int,
    plan: tuple[BlockRead, ...],
    *,
    deadline: Deadline | None,
    command: str,
) -> Mapping[int, int]:
    dl = deadline if deadline is not None else Deadline.after(None, operation=command)
    check = BlockRead(
        function=log.table.read_function,
        address=log.record_address(channel, 0),
        count=log.record_words,
    )
    bank: dict[int, int] = {}
    for attempt in range(2):
        reply = await client.read_plan((*plan, check), deadline=dl, command=command)
        *scan, after = reply.replies
        bank = {a: w for r in scan for a, w in r.to_bank().items()}
        before = scan[0].words[: log.record_words]
        if before == after.words:
            break
        if attempt == 0:
            _LOG.info(
                "%s: %s gained an entry during the read; reading it again", client.label, log.name
            )
    return MappingProxyType(bank)


# --- Clock and A/D ---------------------------------------------------------------------------


async def read_clock(client: ProtocolClient, *, deadline: Deadline | None = None) -> ClockReading:
    """The analyzer's clock (undocumented; design §6.6).

    Raises:
        FujiDecodeError: the words are not a date.
    """
    reply = await client.read(CLOCK_PLAN[0], deadline=deadline, command="read_clock")
    return ClockReading(decode_clock(reply.words), reply.timing)


async def read_adc(client: ProtocolClient, *, deadline: Deadline | None = None) -> AdcValues:
    """The 21 A/D counts of the service manual's table (undocumented; design §6.6).

    A service diagnostic, not a calibrated or higher-resolution gas measurement.
    """
    reply = await client.read(ADC_PLAN[0], deadline=deadline, command="read_adc")
    return decode_adc(
        reply.words,
        received_at=reply.timing.received_at,
        t_mono_ns=reply.timing.midpoint_mono_ns,
    )
