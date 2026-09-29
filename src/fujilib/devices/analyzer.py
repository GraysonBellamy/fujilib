"""The :class:`Analyzer` facade: one ZP-series analyzer on one station (design §7.2).

Every method that reads from the analyzer runs as one operation of the
:class:`~fujilib.devices.session.Session`, under the port's operation lock and
one operation deadline. Arguments are checked first, so a bad channel or
parameter name is refused before anything is sent.

- Every I/O method takes a keyword-only ``timeout``: a deadline for the whole
  operation, including the wait for the port and any retries. ``None`` (the
  default) leaves it to the per-transaction timeout and the retries.
- Channel arguments accept a :class:`~fujilib.registry.channels.ChannelId` or a
  string such as ``"CH3"``.
- Everything that changes the analyzer takes ``confirm``, and is refused
  before anything is sent unless it is ``True`` (design §6.2). A setting write
  is written once and read back (:class:`~fujilib.devices.writes.WriteResult`);
  only the reviewed subset of the register map is writable (design §5.4).

Example::

    async with await open_device(
        "COM8", channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}
    ) as anz:
        frame = await anz.poll()
        o2 = frame.channel("CH3")  # value, unit, state, label source, ...
"""

from __future__ import annotations

import math
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Self

import anyio

from fujilib._deadline import Deadline
from fujilib.devices import operations, reads
from fujilib.devices.capability import (
    OPTION_CAPABILITIES,
    PROBED_CAPABILITIES,
    Availability,
    Capability,
    SafetyTier,
)
from fujilib.devices.encode import prepare_value
from fujilib.devices.operations import CalibrationRun, CalibrationWait
from fujilib.devices.settings import ApplyReport, SettingsDocument, diff_settings
from fujilib.devices.writes import outcome_error
from fujilib.errors import (
    ErrorContext,
    FujiConfirmationRequiredError,
    FujiConnectionError,
    FujiError,
    FujiTimeoutError,
    FujiValidationError,
)
from fujilib.registry.channels import ChannelId, Gas, coerce_channel, coerce_channel_map
from fujilib.registry.enums import RangeIndex
from fujilib.registry.registers import ScalingKind
from fujilib.registry.write_policy import OPERATIONS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from types import TracebackType

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import (
        AdcValues,
        AnalyzerMetadata,
        AnalyzerStatus,
        CalibrationLogEntry,
        ChannelInfo,
        ChannelStatus,
        DeviceInfo,
        ErrorLogEntry,
        Frame,
        RangeInfo,
        Reading,
    )
    from fujilib.devices.operations import CalibrationPlan, CalibrationStatus, CommandResult
    from fujilib.devices.reads import ClockReading
    from fujilib.devices.session import Session
    from fujilib.devices.settings import SettingsDiff
    from fujilib.devices.snapshot import FujiDeviceSnapshot
    from fujilib.devices.writes import WriteResult
    from fujilib.protocol.base import ProtocolClient, ProtocolKind
    from fujilib.registry.enums import ErrorCode, HoldMode, RangeMethod
    from fujilib.registry.registers import RegisterSpec
    from fujilib.registry.units import Unit

__all__ = ["Analyzer"]

#: Alarms 1-6, whose target channels the caller may supply (design §5.2).
_ALARMS = range(1, 7)
#: Response-time slots by name: four NDIR components and O2 (design §2.6).
_RESPONSE_SLOTS = frozenset({"o2", "ndir1", "ndir2", "ndir3", "ndir4"})
_NDIR_SLOTS = 4


class Analyzer:
    """A Fuji ZP-series analyzer. Created by :func:`~fujilib.devices.factory.open_device`."""

    def __init__(self, session: Session) -> None:
        """Wrap an open ``session``; :func:`~fujilib.devices.factory.open_device` does this."""
        self._session = session

    # --- Lifecycle -------------------------------------------------------------------------

    async def close(self) -> None:
        """Close the analyzer and the port it opened. Idempotent.

        Waits for an operation in progress to finish. A transport the caller
        passed to ``open_device`` is left open.
        """
        await self._session.close()

    async def reopen(self, *, timeout: float | None = None) -> DeviceInfo:
        """Open the port again and identify the analyzer, after a connection failure.

        A connection failure breaks the session (every later call is refused);
        this is the way back without losing what the session learned. Only a
        port that ``open_device`` opened by name can be reopened. The station
        must answer as the same analyzer: the same serial number and type code.

        Raises:
            FujiConfigurationError: the port came from the caller; or another
                analyzer answers on the station.
            FujiConnectionError: the analyzer is closed, or the port cannot be opened.
            FujiError: identification failed; the analyzer stays unusable.
        """
        return await self._session.reopen(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # --- What is known, without I/O ------------------------------------------------------

    @property
    def session(self) -> Session:
        """The session: counters, caches and state (unified API §J)."""
        return self._session

    @property
    def info(self) -> DeviceInfo | None:
        """What ``identify()`` established, kept current; ``None`` before it."""
        return self._session.info

    @property
    def channels(self) -> tuple[ChannelInfo, ...]:
        """The established channels, labelled (design §2.9)."""
        return self._session.channels

    @property
    def address(self) -> int:
        """The station number, 1-31."""
        return self._session.address

    @property
    def port(self) -> str:
        """The canonical port name."""
        return self._session.port

    @property
    def protocol(self) -> ProtocolKind:
        """Always :attr:`ProtocolKind.MODBUS_RTU`."""
        return self._session.protocol

    @property
    def last_frame(self) -> Frame | None:
        """The most recent poll's frame, or ``None``."""
        return self._session.last_frame

    @property
    def options(self) -> Capability:
        """The options taken as fitted: asserted when opening, or listed by the type code."""
        return self._session.options

    async def snapshot(self, *, name: str | None = None) -> FujiDeviceSnapshot:
        """Identity and health from cached state, with no I/O (unified API §H).

        ``name`` defaults to the model, or ``"analyzer"`` before ``identify()``.
        """
        return self._session.snapshot(name=name)

    # --- Identity ------------------------------------------------------------------------

    async def identify(
        self,
        *,
        channel_map: Mapping[ChannelId | str, Gas | str] | None = None,
        timeout: float | None = None,
    ) -> DeviceInfo:
        """Read the type code, serial number, ranges and readings, and probe the capabilities.

        Six transactions (design §4.3). ``channel_map`` replaces the asserted
        gas labels; an asserted label is the only one fit for calculation
        (design §2.9).

        Raises:
            FujiValidationError: ``channel_map`` names an unknown channel or gas.
            FujiProtocolUnsupportedError: the type code does not name a ZP model.
            FujiError: a transaction failed.
        """
        asserted = coerce_channel_map(channel_map) if channel_map is not None else None
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> DeviceInfo:
            identity = await session.profile.identify(client, probe=True, deadline=deadline)
            return session.learn_identity(identity, channel_map=asserted)

        return await session.run("identify", body, timeout=timeout)

    async def read_ranges(self, *, timeout: float | None = None) -> tuple[RangeInfo, ...]:
        """Read the range tables of channels 1-5; the session keeps them. One transaction."""
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> tuple[RangeInfo, ...]:
            return session.learn_ranges(await reads.read_ranges(client, deadline=deadline))

        return await session.run("read_ranges", body, timeout=timeout)

    async def read_metadata(self, *, timeout: float | None = None) -> AnalyzerMetadata:
        """The settings snapshot a consumer records with its data (design §2.11, §7.2).

        Response times, averaging, calibration gases and scope, hold, the
        automatic schedules, the current ranges and the analyzer's clock. Reads
        the identity first if ``identify()`` has not run, and the range tables
        if a poll saw a range change.

        Raises:
            FujiError: a transaction failed.
        """
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> AnalyzerMetadata:
            info = session.info
            if info is None:
                identity = await session.profile.identify(client, probe=True, deadline=deadline)
                info = session.learn_identity(identity)
            ranges = await session.ensure_ranges(client, deadline)
            clock = await self._clock_available(client, deadline)
            meta = await reads.read_metadata(
                client,
                serial_number=info.serial_number,
                ranges=ranges,
                channels=session.channels,
                clock=clock,
                deadline=deadline,
            )
            session.learn_current_ranges(meta.current_range)
            return meta

        return await session.run("read_metadata", body, timeout=timeout)

    async def _clock_available(self, client: ProtocolClient, deadline: Deadline) -> bool:
        availability = self._session.availability[Capability.CLOCK]
        if availability is Availability.UNKNOWN:
            probe = await reads.probe_capability(client, Capability.CLOCK, deadline=deadline)
            self._session.set_availability(Capability.CLOCK, probe.availability)
            availability = probe.availability
        return availability is Availability.SUPPORTED

    # --- Measurements --------------------------------------------------------------------

    async def poll(self, *, detail: bool = True, timeout: float | None = None) -> Frame:
        """Read every established channel and the analyzer's status: two transactions.

        Without ``detail`` only the concentrations are read (one transaction),
        and every reading's validity is unknown rather than assumed (design
        §4.3). A channel that reads non-zero for the first time joins the
        established channels, in this frame and every later one (design §2.9).

        Raises:
            FujiError: a transaction failed. When the status block fails after
                the concentrations were read, the error's ``extra["completed"]``
                holds the concentration words.
        """
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> Frame:
            return session.learn_poll(
                await reads.read_poll(client, detail=detail, deadline=deadline)
            )

        return await session.run("poll", body, timeout=timeout)

    async def read_channel(
        self, channel: ChannelId | str, *, timeout: float | None = None
    ) -> Reading:
        """One established channel's reading, from a full poll.

        Raises:
            FujiValidationError: ``channel`` is unknown or not established;
                nothing was sent. Assert it with ``channel_map`` to read it.
        """
        cid = self._established(channel)
        frame = await self.poll(timeout=timeout)
        return frame.channel(cid)

    async def status(self, *, timeout: float | None = None) -> AnalyzerStatus:
        """The analyzer's status: errors, alarms, auto calibration, display. Two transactions."""
        status = await self._read_status("status", timeout)
        return status.analyzer

    async def channel_status(
        self, channel: ChannelId | str, *, timeout: float | None = None
    ) -> ChannelStatus:
        """One measured channel's status: range, calibration, hold and errors. Two transactions.

        Raises:
            FujiValidationError: ``channel`` is not one of channels 1-5; nothing was sent.
        """
        cid = coerce_channel(channel)
        if not cid.is_measured:
            msg = f"{cid.value} is a derived channel and has no status registers"
            raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
        status = await self._read_status("channel_status", timeout)
        return status.channels[cid]

    async def _read_status(self, operation: str, timeout: float | None) -> reads.StatusRead:
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> reads.StatusRead:
            status = await reads.read_status(client, deadline=deadline)
            session.learn_current_ranges({c: s.range for c, s in status.channels.items()})
            return status

        return await session.run(operation, body, timeout=timeout)

    def _established(self, channel: ChannelId | str) -> ChannelId:
        cid = coerce_channel(channel)
        if all(c.channel is not cid for c in self._session.channels):
            msg = (
                f"{cid.value} is not an established channel; assert its gas with "
                "channel_map, or poll until it reads non-zero"
            )
            raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
        return cid

    # --- Diagnostics ---------------------------------------------------------------------

    async def read_clock(self, *, timeout: float | None = None) -> ClockReading:
        """The analyzer's real-time clock and when it was read (undocumented; design §6.6).

        Naive local time with a two-digit year; it drifts, and it never replaces
        host timestamps.

        Raises:
            FujiCapabilityError: the analyzer has no clock; nothing was sent once known.
            FujiDecodeError: the words are not a date.
        """
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> ClockReading:
            return await session.read_capability(
                Capability.CLOCK, "read_clock", lambda: reads.read_clock(client, deadline=deadline)
            )

        return await session.run("read_clock", body, timeout=timeout, requires=Capability.CLOCK)

    async def read_adc(self, *, timeout: float | None = None) -> AdcValues:
        """The 21 raw A/D counts of the service manual's table (undocumented; design §6.6).

        A service diagnostic, not a calibrated or higher-resolution gas measurement.

        Raises:
            FujiCapabilityError: the analyzer has no A/D block; nothing was sent once known.
        """
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> AdcValues:
            return await session.read_capability(
                Capability.ADC_VALUES, "read_adc", lambda: reads.read_adc(client, deadline=deadline)
            )

        return await session.run("read_adc", body, timeout=timeout, requires=Capability.ADC_VALUES)

    async def reprobe(
        self, capability: Capability, *, timeout: float | None = None
    ) -> Availability:
        """Probe ``capability`` again and return what was found (design §6.6).

        Raises:
            FujiValidationError: ``capability`` is not a probed capability; nothing was sent.
        """
        if capability not in PROBED_CAPABILITIES:
            names = ", ".join(str(c.name) for c in PROBED_CAPABILITIES)
            msg = f"{capability!r} is not a probed capability; probed are {names}"
            raise FujiValidationError(msg)
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> Availability:
            probe = await reads.probe_capability(client, capability, deadline=deadline)
            session.set_availability(capability, probe.availability)
            return probe.availability

        return await session.run("reprobe", body, timeout=timeout)

    # --- Logs ----------------------------------------------------------------------------

    async def read_error_log(self, *, timeout: float | None = None) -> tuple[ErrorLogEntry, ...]:
        """The error log, newest first; up to 14 entries with day, hour and minute only."""

        async def body(client: ProtocolClient, deadline: Deadline) -> tuple[ErrorLogEntry, ...]:
            return await reads.read_error_log(client, deadline=deadline)

        return await self._session.run("read_error_log", body, timeout=timeout)

    async def read_calibration_log(
        self, channel: ChannelId | str | None = None, *, timeout: float | None = None
    ) -> tuple[CalibrationLogEntry, ...]:
        """Calibration-log records, newest first per channel; firmware 2.24 or later.

        ``channel`` ``None`` reads every established measured channel, in order.
        Each channel is seven transactions.

        Raises:
            FujiValidationError: ``channel`` is not one of channels 1-5; nothing was sent.
            FujiFirmwareError: the analyzer has no calibration log (older
                firmware); nothing was sent once known.
        """
        if channel is not None:
            wanted: tuple[ChannelId, ...] = (coerce_channel(channel),)
        else:
            wanted = tuple(c.channel for c in self._session.channels if c.channel.is_measured)
        for cid in wanted:
            if not cid.is_measured:
                msg = f"{cid.value} is a derived channel and has no calibration log"
                raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
        session = self._session

        async def body(
            client: ProtocolClient, deadline: Deadline
        ) -> tuple[CalibrationLogEntry, ...]:
            entries: list[CalibrationLogEntry] = []
            for cid in wanted:
                entries += await session.read_capability(
                    Capability.CALIBRATION_LOG,
                    "read_calibration_log",
                    partial(reads.read_calibration_log, client, cid, deadline=deadline),
                )
            return tuple(entries)

        return await session.run(
            "read_calibration_log", body, timeout=timeout, requires=Capability.CALIBRATION_LOG
        )

    # --- Parameters ----------------------------------------------------------------------

    async def read_parameter(
        self,
        name: str,
        *,
        alarm_targets: Mapping[int, ChannelId | str] | None = None,
        timeout: float | None = None,
    ) -> RegisterValue:
        """One register by its name in the register map (``docs/registers.md``).

        A range-scaled value uses the range tables, read first if needed. An
        alarm limit needs ``alarm_targets`` (alarm number to channel), because
        the target register's encoding is contested (design §5.2); without it
        the value is ``None`` and only the raw word is given.

        Raises:
            FujiValidationError: ``name`` is not in the register map; nothing was sent.
        """
        values = await self._read_named("read_parameter", (name,), alarm_targets, timeout)
        return values[name]

    async def read_parameters(
        self,
        names: Iterable[str],
        *,
        alarm_targets: Mapping[int, ChannelId | str] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, RegisterValue]:
        """Several registers by name, read in the fewest blocks, in the order given.

        Raises:
            FujiValidationError: a name is not in the register map; nothing was sent.
        """
        if isinstance(names, str):
            msg = "names must be a sequence of register names, not one string"
            raise FujiValidationError(msg)
        return await self._read_named("read_parameters", tuple(names), alarm_targets, timeout)

    async def read_settings(
        self,
        *,
        alarm_targets: Mapping[int, ChannelId | str] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, RegisterValue]:
        """Every holding register, decoded, by name. Three transactions, plus the ranges if needed.

        Raises:
            FujiError: a transaction failed.
        """
        targets = _alarm_targets(alarm_targets)
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> Mapping[str, RegisterValue]:
            ranges = await session.ensure_ranges(client, deadline)
            return await reads.read_settings(
                client, ranges=ranges, alarm_targets=targets, deadline=deadline
            )

        return await session.run("read_settings", body, timeout=timeout)

    async def _read_named(
        self,
        operation: str,
        names: Sequence[str],
        alarm_targets: Mapping[int, ChannelId | str] | None,
        timeout: float | None,
    ) -> Mapping[str, RegisterValue]:
        session = self._session
        specs = [session.profile.registry.resolve(n) for n in names]
        targets = _alarm_targets(alarm_targets)
        scaled = any(
            s.scaling.kind in {ScalingKind.BY_RANGE, ScalingKind.BY_ALARM_TARGET} for s in specs
        )
        requires = Capability.NONE
        for spec in specs:
            # An option never gates a read: its registers read whether it is fitted or not.
            requires |= spec.requires & ~OPTION_CAPABILITIES
        probed = [c for c in PROBED_CAPABILITIES if c in requires]

        async def body(client: ProtocolClient, deadline: Deadline) -> Mapping[str, RegisterValue]:
            ranges = await session.ensure_ranges(client, deadline) if scaled else ()
            read = partial(
                reads.read_registers,
                client,
                names,
                ranges=ranges,
                alarm_targets=targets,
                deadline=deadline,
            )
            if len(probed) == 1:
                # Registers of one probed block: what the read finds updates its availability.
                return await session.read_capability(probed[0], operation, read)
            return await read()

        return await session.run(operation, body, timeout=timeout, requires=requires)

    # --- Settings ------------------------------------------------------------------------

    async def write_parameter(
        self,
        name: str,
        value: object,
        *,
        unit: Unit | str | None = None,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Write one setting by its name in the register map, then read it back (design §6.3).

        Only the reviewed subset is writable (``docs/registers.md``, design
        §5.4). ``value`` is ``True``/``False`` for a flag, an enum member or its
        name for an enumerated setting, a whole number for a time or count, and
        for a calibration gas a number in ``unit``, which is then required and
        must be the unit of the gas's (channel, range).

        Before the write the analyzer's status is read, and the write refused
        while a calibration runs or the front panel is in a menu. The write is
        sent once, never retried, and read back whatever happens to its reply.
        About six transactions.

        Raises:
            FujiValidationError: an unknown or read-only name, or a value that
                does not fit; nothing was written.
            FujiConfirmationRequiredError: ``confirm`` is not ``True``; nothing
                was sent.
            FujiAnalyzerStateError: a calibration is running or the panel is in
                a menu; nothing was written.
            FujiModbusError: the analyzer refused the write; nothing was applied.
            FujiVerificationError: the setting reads back as something else.
            FujiWriteOutcomeUnknownError: neither the write's reply nor the
                read-back arrived; the write may or may not have been applied.
        """
        spec = self._writable(name)
        return await self._write(
            "write_parameter", spec, value, unit=unit, confirm=confirm, timeout=timeout
        )

    async def set_response_time(
        self,
        target: ChannelId | str,
        seconds: int,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Set a response time, 1-60 s, by channel or by slot (``"o2"``, ``"ndir1"``..``"ndir4"``).

        There are four NDIR-component slots and one O2 slot, not one per
        channel (design §2.6). A channel is mapped to its slot only through
        asserted gases: O2 to the O2 slot, the n-th NDIR channel to ``ndirn``,
        which needs every channel before it asserted. Otherwise name the slot.

        Raises:
            FujiValidationError: the channel cannot be mapped to a slot; as
                :meth:`write_parameter` otherwise.
            FujiError: as :meth:`write_parameter`.
        """
        name = self._response_time_name(target)
        return await self._write(
            "set_response_time", self._writable(name), seconds, confirm=confirm, timeout=timeout
        )

    async def set_output_hold(
        self, enabled: bool, *, confirm: bool = False, timeout: float | None = None
    ) -> WriteResult:
        """Hold the outputs, and the Modbus concentrations, during calibration, or not.

        Raises:
            FujiError: as :meth:`write_parameter`.
        """
        spec = self._writable("output_hold.enabled")
        return await self._write("set_output_hold", spec, enabled, confirm=confirm, timeout=timeout)

    async def set_hold_mode(
        self, mode: HoldMode | str, *, confirm: bool = False, timeout: float | None = None
    ) -> WriteResult:
        """What the outputs hold during calibration: ``last_value`` or ``setting``.

        Raises:
            FujiError: as :meth:`write_parameter`.
        """
        spec = self._writable("hold.mode")
        return await self._write("set_hold_mode", spec, mode, confirm=confirm, timeout=timeout)

    async def set_hold_value(
        self,
        channel: ChannelId | str,
        percent_fs: int,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """The value a measured channel holds in ``setting`` mode, 0-100 % of full scale.

        Raises:
            FujiValidationError: ``channel`` is not one of channels 1-5.
            FujiError: as :meth:`write_parameter`.
        """
        cid = _measured(channel)
        spec = self._writable(f"hold.ch{cid.number}.value")
        return await self._write(
            "set_hold_value", spec, percent_fs, confirm=confirm, timeout=timeout
        )

    async def set_range(
        self,
        channel: ChannelId | str,
        range_number: int,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Select range 1 or 2 of a measured channel, whose range method must be manual.

        It returns once the channel measures on the range: the analyzer
        switches some tens of milliseconds after the setting reads back, so
        the channel's current range is read until it follows, within the
        read-back budget.

        Raises:
            FujiValidationError: ``channel`` is not one of channels 1-5,
                ``range_number`` is not 1 or 2, or the channel's range method
                is not manual; nothing was written.
            FujiVerificationError: the setting reads back otherwise, or it
                reads back as written but the channel did not switch to it.
            FujiError: as :meth:`write_parameter`.
        """
        cid = _measured(channel)
        if range_number not in {1, 2} or isinstance(range_number, bool):
            msg = f"range_number must be 1 or 2, got {range_number!r}"
            raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
        spec = self._writable(f"range.ch{cid.number}.selected")
        index = RangeIndex(range_number - 1)
        return await self._write("set_range", spec, index, confirm=confirm, timeout=timeout)

    async def set_range_method(
        self,
        channel: ChannelId | str,
        method: RangeMethod | str,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """How a measured channel changes range: ``manual`` or ``auto`` (``remote`` is refused).

        Raises:
            FujiValidationError: ``channel`` is not one of channels 1-5, or the
                method is not ``manual`` or ``auto``.
            FujiError: as :meth:`write_parameter`.
        """
        cid = _measured(channel)
        spec = self._writable(f"range.ch{cid.number}.method")
        return await self._write("set_range_method", spec, method, confirm=confirm, timeout=timeout)

    async def set_calibration_gas(
        self,
        channel: ChannelId | str,
        range_number: int,
        kind: str,
        value: float | str,
        *,
        unit: Unit | str,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Set the zero or span calibration gas of a measured channel's range. DANGEROUS.

        It takes effect at the next calibration, manual or automatic, and a
        wrong value miscalibrates the analyzer then. ``unit`` must be the
        range's own; span gas is limited to 1-105 % and zero gas to 0-100 % of
        the range's full scale.

        Raises:
            FujiValidationError: ``channel``, ``range_number`` or ``kind``
                (``"zero"`` or ``"span"``) is not one there is, or the value
                does not fit the range.
            FujiError: as :meth:`write_parameter`.
        """
        cid = _measured(channel)
        if (
            range_number not in {1, 2}
            or isinstance(range_number, bool)
            or kind
            not in {
                "zero",
                "span",
            }
        ):
            msg = f"expected range 1 or 2 and kind 'zero' or 'span', got {range_number!r}, {kind!r}"
            raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
        spec = self._writable(f"calibration_gas.ch{cid.number}.range{range_number}.{kind}")
        return await self._write(
            "set_calibration_gas", spec, value, unit=unit, confirm=confirm, timeout=timeout
        )

    async def diff_settings(
        self,
        document: SettingsDocument | Mapping[str, object],
        *,
        any_analyzer: bool = False,
        timeout: float | None = None,
    ) -> SettingsDiff:
        """Compare a settings document (``fujilib-settings/1``) with the analyzer's settings.

        Read-only: the range tables and every setting are read (four
        transactions), and each setting of the document is found unchanged, to
        be written, or refused, with the reason (:mod:`fujilib.devices.settings`).
        A document from another analyzer is refused unless ``any_analyzer``.

        Raises:
            FujiValidationError: the document is not a settings document.
            FujiError: a transaction failed.
        """
        doc = (
            document
            if isinstance(document, SettingsDocument)
            else SettingsDocument.from_json(dict(document))
        )
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> SettingsDiff:
            info = session.info
            if info is None:
                identity = await session.profile.identify(client, probe=True, deadline=deadline)
                info = session.learn_identity(identity)
            ranges = session.learn_ranges(await reads.read_ranges(client, deadline=deadline))
            current = await reads.read_settings(client, ranges=ranges, deadline=deadline)
            return diff_settings(
                doc,
                current,
                registry=session.profile.registry,
                ranges=ranges,
                serial_number=info.serial_number,
                any_analyzer=any_analyzer,
            )

        return await session.run("diff_settings", body, timeout=timeout)

    async def apply_settings(
        self,
        document: SettingsDocument | Mapping[str, object],
        *,
        confirm: bool = False,
        any_analyzer: bool = False,
        max_tier: SafetyTier = SafetyTier.DANGEROUS,
        timeout: float | None = None,
    ) -> ApplyReport:
        """Write the settings of a document that differ from the analyzer's.

        The whole document is compared first (:meth:`diff_settings`); if any
        setting is refused, nothing is written. ``confirm`` must then be
        ``True``; the CLI also asks for its destructive flag when a write is
        DANGEROUS. The writes go one at a time in dependency order, each read
        back; the first that fails stops the rest, and nothing is rolled back.
        ``max_tier`` refuses a document whose writes go above it, judged on
        the comparison made here, not on an earlier one: the CLI passes
        ``PERSISTENT`` unless its destructive flag is given. ``timeout`` bounds
        the whole apply.

        Raises:
            FujiValidationError: the document is not a settings document, or is
                refused; nothing was written.
            FujiConfirmationRequiredError: there is something to write and
                ``confirm`` is not ``True``, or a write is above ``max_tier``;
                nothing was written.
            FujiError: the comparison's reads failed. A failed *write* does not
                raise: it is in the report.
        """
        deadline = Deadline.after(timeout, operation="apply_settings")
        diff = await self.diff_settings(
            document, any_analyzer=any_analyzer, timeout=_remaining(deadline)
        )
        if not diff.ok:
            raise diff.refusal()
        if not diff.writes:
            return ApplyReport(diff, ())
        if diff.tier > max_tier:
            msg = (
                f"applying the settings would make {diff.tier.name} writes, above "
                f"{max_tier.name}; nothing was written"
            )
            raise FujiConfirmationRequiredError(
                msg, context=ErrorContext(extra={"safety": diff.tier.name.lower()})
            )
        self._session.gate(
            "apply_settings", tier=diff.tier, confirm=confirm, subject="applying the settings"
        )
        registry = self._session.profile.registry
        completed: list[WriteResult] = []
        writes = diff.writes
        for index, change in enumerate(writes):
            spec = registry.resolve(change.name)
            try:
                result = await self._write(
                    "apply_settings",
                    spec,
                    change.desired.value,
                    unit=change.desired.unit,
                    confirm=confirm,
                    timeout=_remaining(deadline),
                )
            except FujiError as exc:
                rest = tuple(c.name for c in writes[index + 1 :])
                return ApplyReport(diff, tuple(completed), change.name, exc, rest)
            completed.append(result)
        return ApplyReport(diff, tuple(completed))

    # --- Operations ----------------------------------------------------------------------

    async def calibration_status(self, *, timeout: float | None = None) -> CalibrationStatus:
        """What is calibrating, held or failed. Two transactions, the poll's status blocks."""
        status = await self._read_status("calibration_status", timeout)
        return operations.calibration_status(status)

    async def plan_auto_calibration(self, *, timeout: float | None = None) -> CalibrationPlan:
        """What :meth:`start_auto_calibration` would calibrate, against which gases, for how long.

        Read-only: the channels enabled for it, their ranges (both where the
        calibration range is "both"), the calibration gases, whether the
        outputs are held, and an estimated duration inferred from the flow
        times. Two transactions, plus the range tables if needed.
        """
        return await self._plan("plan_auto_calibration", CalibrationRun.AUTO_CALIBRATION, timeout)

    async def plan_auto_zero_calibration(self, *, timeout: float | None = None) -> CalibrationPlan:
        """What :meth:`start_auto_zero_calibration` would zero; as :meth:`plan_auto_calibration`."""
        return await self._plan("plan_auto_zero_calibration", CalibrationRun.AUTO_ZERO, timeout)

    async def start_auto_calibration(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Run auto calibration once (42003). DANGEROUS: it overwrites the calibration.

        It zeroes and spans every channel enabled for it against the
        calibration gases the analyzer's own valves let in, so it is right only
        where those gases are plumbed. It needs the auto-calibration option,
        which the type code lists or ``open_device(options=...)`` asserts. It is
        refused while a calibration runs, the panel is in a menu, or the
        analyzer reports an instrument error. The plan is read and returned
        with the result; no register stops a calibration once started.

        Raises:
            FujiConfirmationRequiredError: ``confirm`` is not ``True``; nothing was sent.
            FujiCapabilityError: the option is not fitted; nothing was sent.
            FujiAnalyzerStateError: the analyzer's state forbids it; nothing was sent.
            FujiWriteOutcomeUnknownError: its reply was lost and the status
                does not show it running.
            FujiError: a transaction failed.
        """
        return await self._command(
            "start_auto_calibration", CalibrationRun.AUTO_CALIBRATION, confirm, timeout
        )

    async def start_auto_zero_calibration(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Run auto zero calibration once (42004). DANGEROUS; as :meth:`start_auto_calibration`.

        It zeroes every channel enabled for auto calibration, and needs the
        auto-zero option.
        """
        return await self._command(
            "start_auto_zero_calibration", CalibrationRun.AUTO_ZERO, confirm, timeout
        )

    async def start_blowback(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Run blowback once (42005). STATEFUL; the blowback option, which no ZPA has.

        No register shows blowback running, so the outcome is only ``sent``.

        Raises:
            FujiCapabilityError: the analyzer has no blowback; nothing was sent.
            FujiError: as :meth:`start_auto_calibration`.
        """
        return await self._command("start_blowback", None, confirm, timeout)

    async def return_to_measurement(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Put the front panel back on the measurement screen (42002). STATEFUL.

        It takes an operator out of whatever menu they are in, so it is not
        refused while the panel is in one. It does not stop a calibration.

        Raises:
            FujiConfirmationRequiredError: ``confirm`` is not ``True``; nothing was sent.
            FujiVerificationError: acknowledged, but the panel does not show
                the measurement screen.
            FujiWriteOutcomeUnknownError: its reply was lost and the status
                cannot be read.
            FujiError: a transaction failed.
        """
        return await self._command("return_to_measurement", None, confirm, timeout)

    async def wait_for_calibration(
        self,
        *,
        timeout: float,
        interval: float = 2.0,
        since: CalibrationStatus | None = None,
    ) -> CalibrationWait:
        """Wait until nothing is calibrating, reading the status every ``interval`` seconds.

        The port is free between reads, so a recording goes on meanwhile.
        ``timeout`` bounds the whole wait. The result says whether a
        calibration was seen running at all (if not, it may have ended
        already), whether any error 4-9 is active at the end, and which
        appeared since ``since``: pass the ``before`` of the command's result,
        or the wait's own first read is the baseline.

        Raises:
            FujiValidationError: ``timeout`` or ``interval`` is not a positive number.
            FujiTimeoutError: something was still calibrating at ``timeout``; its
                context says whether a calibration was seen and how many reads
                were made.
            FujiError: a status read failed.
        """
        _check_seconds("timeout", timeout)
        _check_seconds("interval", interval)
        deadline = Deadline.after(timeout, operation="wait_for_calibration")
        saw_running = False
        polls = 0
        try:
            with deadline.enforce():
                first = await self.calibration_status()
                status = first
                while True:
                    polls += 1
                    if not status.busy:
                        return CalibrationWait(
                            final=status,
                            saw_running=saw_running,
                            polls=polls,
                            elapsed_s=deadline.elapsed(),
                            new_errors=_new_errors((since or first).errors, status.errors),
                        )
                    saw_running = True
                    await anyio.sleep(interval)
                    status = await self.calibration_status()
        except FujiTimeoutError as exc:
            raise exc.with_context(saw_running=saw_running, polls=polls) from exc.__cause__

    async def _plan(
        self, operation: str, run: CalibrationRun, timeout: float | None
    ) -> CalibrationPlan:
        session = self._session

        async def body(client: ProtocolClient, deadline: Deadline) -> CalibrationPlan:
            ranges = await session.ensure_ranges(client, deadline)
            return await operations.read_calibration_plan(
                client,
                run,
                ranges=ranges,
                established=[c.channel for c in session.channels],
                deadline=deadline,
            )

        return await session.run(operation, body, timeout=timeout)

    async def _command(
        self,
        name: str,
        run: CalibrationRun | None,
        confirm: bool,
        timeout: float | None,
    ) -> CommandResult:
        spec = OPERATIONS[name]
        session = self._session
        session.gate(name, tier=spec.safety, confirm=confirm, requires=spec.requires)

        async def body(client: ProtocolClient, deadline: Deadline) -> CommandResult:
            plan = None
            before = None
            if name != "return_to_measurement":
                status = await session.check_quiet(client, deadline, name)
                before = operations.calibration_status(status)
                if run is not None:
                    operations.check_healthy(status, name)
                    ranges = await session.ensure_ranges(client, deadline)
                    plan = await operations.read_calibration_plan(
                        client,
                        run,
                        ranges=ranges,
                        established=[c.channel for c in session.channels],
                        deadline=deadline,
                    )
            result = await operations.send_command(
                client,
                spec,
                plan=plan,
                before=before,
                deadline=deadline,
                verify_timeout=session.verify_timeout,
            )
            if isinstance(result.status_error, FujiConnectionError):
                session.note_port_failure(result.status_error)
            return result

        return await session.run(
            name, body, timeout=timeout, tier=spec.safety, confirm=confirm, requires=spec.requires
        )

    def _writable(self, name: str) -> RegisterSpec:
        spec = self._session.profile.registry.resolve(name)
        if not spec.writable:
            msg = (
                f"{name} is read-only: fujilib writes only the reviewed subset of the "
                "register map (docs/registers.md, design §5.4)"
            )
            raise FujiValidationError(msg, context=ErrorContext(extra={"setting": name}))
        return spec

    async def _write(
        self,
        operation: str,
        spec: RegisterSpec,
        value: object,
        *,
        unit: Unit | str | None = None,
        confirm: bool,
        timeout: float | None,
    ) -> WriteResult:
        session = self._session
        tier, requires = spec.safety, spec.requires
        session.gate(
            operation, tier=tier, confirm=confirm, requires=requires, subject=f"writing {spec.name}"
        )
        prepared = prepare_value(spec, value, unit=unit)

        async def body(client: ProtocolClient, deadline: Deadline) -> WriteResult:
            result = await session.write_setting(client, deadline, prepared, command=operation)
            error = outcome_error(result)
            if error is not None:
                raise error
            return result

        return await session.run(
            operation, body, timeout=timeout, tier=tier, confirm=confirm, requires=requires
        )

    def _response_time_name(self, target: ChannelId | str) -> str:
        slot_name = target.strip().lower()
        if slot_name in _RESPONSE_SLOTS:
            return f"response_time.{slot_name}"
        cid = _measured(target)
        asserted = self._session.asserted
        if asserted.get(cid) is Gas.O2:
            return "response_time.o2"
        slot = 0
        for number in range(1, cid.number + 1):
            gas = asserted.get(ChannelId.from_number(number))
            if gas is None:
                msg = (
                    f"{cid.value}'s response-time slot follows from the asserted gases of "
                    f"channels 1-{cid.number}, and CH{number} has none; name the slot "
                    "instead: 'o2' or 'ndir1'-'ndir4'"
                )
                raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
            if gas is not Gas.O2:
                slot += 1
        if slot > _NDIR_SLOTS:
            msg = f"{cid.value} would be NDIR component {slot}; there are only four"
            raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
        return f"response_time.ndir{slot}"

    def __repr__(self) -> str:
        info = self._session.info
        model = info.model if info is not None else "unidentified"
        return f"<Analyzer {model} on {self.port} station {self.address}>"


def _remaining(deadline: Deadline) -> float | None:
    return max(deadline.remaining(), 0.0) if deadline.bounded else None


def _check_seconds(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        msg = f"{name} must be a positive number of seconds, got {value!r}"
        raise FujiValidationError(msg)


def _new_errors(
    before: Mapping[ChannelId, frozenset[ErrorCode]],
    after: Mapping[ChannelId, frozenset[ErrorCode]],
) -> Mapping[ChannelId, frozenset[ErrorCode]]:
    new = {c: codes - before.get(c, frozenset()) for c, codes in after.items()}
    return MappingProxyType({c: codes for c, codes in new.items() if codes})


def _measured(channel: ChannelId | str) -> ChannelId:
    cid = coerce_channel(channel)
    if not cid.is_measured:
        msg = f"{cid.value} is a derived channel; only channels 1-5 have this setting"
        raise FujiValidationError(msg, context=ErrorContext(channel=cid.value))
    return cid


def _alarm_targets(
    targets: Mapping[int, ChannelId | str] | None,
) -> Mapping[int, ChannelId] | None:
    if targets is None:
        return None
    out: dict[int, ChannelId] = {}
    for alarm, channel in targets.items():
        if alarm not in _ALARMS:
            msg = f"alarm numbers are 1-6, got {alarm!r}"
            raise FujiValidationError(msg)
        out[alarm] = coerce_channel(channel)
    return out
