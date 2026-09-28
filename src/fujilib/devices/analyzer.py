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
- Nothing here writes to the analyzer.

Example::

    async with await open_device(
        "COM8", channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}
    ) as anz:
        frame = await anz.poll()
        o2 = frame.channel("CH3")  # value, unit, state, label source, ...
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Self

from fujilib.devices import reads
from fujilib.devices.capability import PROBED_CAPABILITIES, Availability, Capability
from fujilib.errors import ErrorContext, FujiValidationError
from fujilib.registry.channels import ChannelId, coerce_channel, coerce_channel_map
from fujilib.registry.registers import ScalingKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from types import TracebackType

    from fujilib._deadline import Deadline
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
    from fujilib.devices.reads import ClockReading
    from fujilib.devices.session import Session
    from fujilib.devices.snapshot import FujiDeviceSnapshot
    from fujilib.protocol.base import ProtocolClient, ProtocolKind
    from fujilib.registry.channels import Gas

__all__ = ["Analyzer"]

#: Alarms 1-6, whose target channels the caller may supply (design §5.2).
_ALARMS = range(1, 7)


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
            requires |= spec.requires
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

    def __repr__(self) -> str:
        info = self._session.info
        model = info.model if info is not None else "unidentified"
        return f"<Analyzer {model} on {self.port} station {self.address}>"


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
