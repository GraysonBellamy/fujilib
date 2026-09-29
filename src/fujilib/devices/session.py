"""The session: the only path from the facade to the wire (design §6).

Every I/O call of an :class:`~fujilib.devices.analyzer.Analyzer` goes through
:meth:`Session.run`, which walks the gates (:meth:`Session.gate`) before
anything is sent (design §6.1):

1. **State.** The session is open, and no connection failure has broken it.
2. **Safety tier.** Anything above ``READ_ONLY`` needs ``confirm=True``.
3. **Capability.** An operation that needs a probed capability is refused
   while that capability is known to be ``UNSUPPORTED`` (design §6.6), and
   one that needs an option is refused unless the type code lists it or the
   caller asserted it (:attr:`Session.options`).

The facade resolves names and checks access before the gates, and checks
values after them, still before any I/O. ``run`` then starts the operation
deadline, which also covers the wait for the port's operation lock (design
§6.4), and holds the lock for the whole operation, so its transactions are
never interleaved with other traffic on the port. A failure is kept as
:attr:`Session.last_error`. A connection failure, or a write whose outcome is
unknown because the port failed, also breaks the session: every later call
is refused until the analyzer is opened again.

A setting write (:meth:`Session.write_setting`) then reads the analyzer's
status and refuses to write while a calibration runs or the front panel is in
a menu; reads the setting and what it depends on; for a range-scaled value,
reads the range it is scaled by; encodes the value; writes it once and reads
it back (:mod:`fujilib.devices.writes`); and, for a channel's selected range,
waits until the channel measures on it.

The session keeps what it has learned about the station (design §6.7):

| Cache | Filled by | Changed by |
|---|---|---|
| identity (``DeviceInfo``) | ``identify()`` | ``identify()``; what the rows below learn |
| established channels | the caller's assertion; a non-zero reading | only ever grows |
| ranges | ``identify()``, ``read_ranges()`` | re-read when a poll sees a current range change |
| availability per capability | the probes | ``reprobe()``; every read of the capability |
| last frame | ``poll()`` | the next ``poll()`` |

The front panel stays live, so none of this stays authoritative for long:
an operator can change a range or a setting at any time (design §1).

A session whose port was opened by name can be reopened after a connection
failure (:meth:`Session.reopen`): the port is opened again under the same
settings, the analyzer is identified again and must be the same one, and
what the session had learned is kept.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio

from fujilib._deadline import Deadline
from fujilib._lock import maybe_acquire
from fujilib._logging import get_logger
from fujilib.config import DEFAULTS
from fujilib.devices._write_rate import WriteRateMonitor
from fujilib.devices.capability import (
    OPTION_CAPABILITIES,
    PROBED_CAPABILITIES,
    Availability,
    Capability,
    SafetyTier,
)
from fujilib.devices.decode import label_channels
from fujilib.devices.encode import encode_prepared
from fujilib.devices.models import DeviceHealth, DeviceInfo
from fujilib.devices.reads import read_ranges, read_registers, read_status
from fujilib.devices.snapshot import FujiDeviceSnapshot
from fujilib.devices.writes import busy_reasons, describe, write_setting
from fujilib.errors import (
    ErrorContext,
    FujiAnalyzerStateError,
    FujiCapabilityError,
    FujiConfigurationError,
    FujiConfirmationRequiredError,
    FujiConnectionError,
    FujiDecodeError,
    FujiError,
    FujiFirmwareError,
    FujiModbusIllegalDataAddressError,
    FujiProtocolUnsupportedError,
    FujiValidationError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.client import FailureKind
from fujilib.registry.enums import RangeIndex, RangeMethod
from fujilib.registry.registers import ScalingKind
from fujilib.registry.typecode import MODEL_OPTIONS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.encode import PreparedValue
    from fujilib.devices.models import ChannelInfo, Frame, RangeInfo
    from fujilib.devices.profile import DeviceProfile
    from fujilib.devices.reads import Identity, PollRead, StatusRead
    from fujilib.devices.writes import WriteResult
    from fujilib.protocol.base import ProtocolClient
    from fujilib.protocol.modbus.client import ClientCounters, ModbusClient
    from fujilib.protocol.modbus.port import ModbusPort
    from fujilib.registry.channels import ChannelId, Gas
    from fujilib.registry.registers import RegisterSpec
    from fujilib.registry.typecode import TypeCode
    from fujilib.registry.units import Unit
    from fujilib.transport.base import SerialSettings

__all__ = ["Reopener", "Session", "SessionState", "describe_identity"]

_LOG = get_logger("session")

type Reopener = Callable[[], Awaitable[ModbusPort]]
"""Opens the session's port again, as it was first opened."""

#: Capabilities that come with a firmware version rather than an option (design §6.6).
_FIRMWARE_CAPABILITIES: Final = frozenset({Capability.TYPE_CODE_EXT, Capability.CALIBRATION_LOG})

#: A setting written only while another reads a given value (ZPA manual p.40).
_REQUIRES_SETTING: Final[Mapping[str, tuple[str, RangeMethod]]] = MappingProxyType(
    {f"range.ch{c}.selected": (f"range.ch{c}.method", RangeMethod.MANUAL) for c in range(1, 6)}
)

#: The register that shows a selected range in effect. The analyzer switches
#: some tens of milliseconds after the setting reads back (protocol findings §13.2).
_CURRENT_RANGE: Final[Mapping[str, str]] = MappingProxyType(
    {f"range.ch{c}.selected": f"range.ch{c}.current" for c in range(1, 6)}
)

#: Seconds between reads of a channel's current range while it has yet to follow.
_RANGE_POLL_S: Final = 0.05


def _option_name(capability: Capability) -> str:
    return (capability.name or str(capability)).lower().replace("_", " ")


class SessionState(StrEnum):
    """Whether a session can talk to its analyzer."""

    OPEN = "open"
    BROKEN = "broken"
    """A connection failure: the analyzer has to be opened again."""
    CLOSED = "closed"


def describe_identity(
    identity: Identity,
    *,
    address: int,
    serial_settings: SerialSettings,
    asserted: Mapping[ChannelId, Gas] | None = None,
    established: Iterable[ChannelId] = (),
    availability: Mapping[Capability, Availability] | None = None,
) -> DeviceInfo:
    """The :class:`DeviceInfo` of an identity read (design §2.9, §6.6).

    ``established`` adds channels seen earlier; ``availability`` overrides
    what the identity's own probes found.

    Raises:
        FujiProtocolUnsupportedError: the type code does not name a ZP model.
    """
    type_code = identity.type_code
    if type_code.model is None:
        msg = f"the analyzer's type code {type_code.raw!r} does not name a ZP-series model"
        raise FujiProtocolUnsupportedError(msg, context=ErrorContext(address=address))
    found = dict(identity.availability)
    found.update(availability or {})
    channels = label_channels(
        set(identity.nonzero) | set(established), asserted=asserted, type_code=type_code
    )
    return DeviceInfo(
        model=type_code.model,
        type_code=type_code,
        serial_number=identity.serial_number,
        channels=channels,
        ranges=identity.ranges,
        capabilities=_supported(found),
        availability=MappingProxyType(found),
        protocol=ProtocolKind.MODBUS_RTU,
        address=address,
        serial_settings=serial_settings,
        health=_health(type_code, found),
    )


def _supported(availability: Mapping[Capability, Availability]) -> Capability:
    flags = Capability.NONE
    for capability, found in availability.items():
        if found is Availability.SUPPORTED:
            flags |= capability
    return flags


def _health(type_code: TypeCode, availability: Mapping[Capability, Availability]) -> DeviceHealth:
    """``PARTIAL`` when a probe had no definite answer or the type code's table is unknown."""
    if not type_code.decoded or Availability.UNKNOWN in availability.values():
        return DeviceHealth.PARTIAL
    return DeviceHealth.OK


class Session:
    """One station's session: gates, the operation lock, deadlines and caches.

    Created by :func:`~fujilib.devices.factory.open_device`; reached as
    :attr:`Analyzer.session <fujilib.devices.analyzer.Analyzer.session>`.
    """

    def __init__(
        self,
        port: ModbusPort,
        *,
        address: int,
        profile: DeviceProfile,
        channel_map: Mapping[ChannelId, Gas] | None = None,
        reopener: Reopener | None = None,
        options: Capability = Capability.NONE,
        verify_timeout: float | None = None,
        write_warn_per_minute: int = DEFAULTS.write_warn_per_minute,
    ) -> None:
        """Bind to station ``address`` on ``port``, which the session then owns.

        ``reopener`` opens the port again for :meth:`reopen`; without one the
        session cannot be reopened. ``options`` are options the caller asserts
        are fitted, whatever the type code says. ``verify_timeout`` bounds the
        read after a write or command; by default it is what the port's timing
        allows two block reads to take, each with its retries and a late-reply
        window. ``write_warn_per_minute`` is where the write-rate warning
        starts (0 for never).

        Raises:
            FujiValidationError: ``address`` is not a station number, 1-31;
                ``options`` holds something that is not an option.
        """
        if options & ~OPTION_CAPABILITIES:
            msg = f"options must be option capabilities, got {options!r}"
            raise FujiValidationError(msg)
        self._client = port.client(address)
        self._port = port
        self._reopener = reopener
        self._reopening = anyio.Lock()
        self._profile = profile
        self._asserted: Mapping[ChannelId, Gas] = MappingProxyType(dict(channel_map or {}))
        self._state = SessionState.OPEN
        self._info: DeviceInfo | None = None
        self._seen: frozenset[ChannelId] = frozenset()
        self._channels: tuple[ChannelInfo, ...] = ()
        self._ranges: tuple[RangeInfo, ...] | None = None
        self._ranges_stale = False
        self._current_ranges: Mapping[ChannelId, int] | None = None
        self._availability = dict.fromkeys(PROBED_CAPABILITIES, Availability.UNKNOWN)
        self._last_frame: Frame | None = None
        self._last_error: ErrorContext | None = None
        self._warned: set[tuple[ChannelId, Gas, Gas]] = set()
        self._asserted_options = options
        self._verify_timeout = (
            verify_timeout if verify_timeout is not None else _read_budget(port, blocks=2)
        )
        self._write_rate = WriteRateMonitor(warn_per_minute=write_warn_per_minute)
        self._relabel()

    # --- State ---------------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        """Open, broken by a connection failure, or closed."""
        return self._state

    @property
    def connected(self) -> bool:
        """Whether the session is open and its transport is too."""
        return self._state is SessionState.OPEN and self._port.transport.is_open

    @property
    def profile(self) -> DeviceProfile:
        """The analyzer family."""
        return self._profile

    @property
    def address(self) -> int:
        """The station number, 1-31."""
        return self._client.address

    @property
    def port(self) -> str:
        """The canonical port name."""
        return self._port.label

    @property
    def protocol(self) -> ProtocolKind:
        """Always :attr:`ProtocolKind.MODBUS_RTU`."""
        return self._port.protocol

    @property
    def serial_settings(self) -> SerialSettings:
        """The serial settings in use."""
        return self._port.transport.settings

    @property
    def reopenable(self) -> bool:
        """Whether :meth:`reopen` can open the port again: it was opened by name."""
        return self._reopener is not None

    @property
    def counters(self) -> ClientCounters:
        """The station's traffic counters: requests, retries and failures by kind.

        Live, and counted over the whole session: after :meth:`reopen` the same
        object goes on counting.
        """
        return self._client.counters

    @property
    def recoverable_error_count(self) -> int:
        """Failed read attempts that a retry of the same read recovered (unified API §J).

        Counted over the whole session, across :meth:`reopen`.
        """
        return self._client.recoverable_error_count

    @property
    def last_error(self) -> ErrorContext | None:
        """The context of the most recent failure, or ``None``."""
        return self._last_error

    # --- What has been learned -----------------------------------------------------------

    @property
    def info(self) -> DeviceInfo | None:
        """What ``identify()`` established, kept current; ``None`` before it."""
        return self._info

    @property
    def asserted(self) -> Mapping[ChannelId, Gas]:
        """The caller's channel map."""
        return self._asserted

    @property
    def channels(self) -> tuple[ChannelInfo, ...]:
        """The established channels, labelled (design §2.9)."""
        return self._channels

    @property
    def ranges(self) -> tuple[RangeInfo, ...] | None:
        """The range tables last read, or ``None``."""
        return self._ranges

    @property
    def current_ranges(self) -> Mapping[ChannelId, int] | None:
        """The range each of channels 1-5 was last seen measuring on, or ``None``."""
        return self._current_ranges

    @property
    def availability(self) -> Mapping[Capability, Availability]:
        """What is known about each probed capability."""
        return MappingProxyType(dict(self._availability))

    @property
    def last_frame(self) -> Frame | None:
        """The most recent poll's frame, or ``None``."""
        return self._last_frame

    @property
    def asserted_options(self) -> Capability:
        """The options the caller asserted when opening the analyzer."""
        return self._asserted_options

    @property
    def options(self) -> Capability:
        """The options taken as fitted: asserted, or listed by the type code.

        An option the model's manual does not describe at all is never taken
        as fitted, even when asserted.
        """
        info = self._info
        listed = info.type_code.options if info is not None else None
        options = self._asserted_options | (listed or Capability.NONE)
        possible = MODEL_OPTIONS.get(info.model) if info is not None else None
        return options & possible if possible is not None else options

    @property
    def verify_timeout(self) -> float:
        """Seconds the read after a write or command may take, whatever the deadline.

        Design §6.4.
        """
        return self._verify_timeout

    @property
    def write_rate(self) -> WriteRateMonitor:
        """The setting writes of the last minute (design §6.3)."""
        return self._write_rate

    def snapshot(self, *, name: str | None = None) -> FujiDeviceSnapshot:
        """Identity and health from what is cached; no I/O (unified API §H).

        ``name`` defaults to the model, or ``"analyzer"`` before ``identify()``.
        """
        info = self._info
        model = info.model if info is not None else None
        return FujiDeviceSnapshot(
            name=name if name is not None else (model or "analyzer"),
            model=model,
            firmware=None,
            serial=info.serial_number if info is not None else None,
            connected=self.connected,
            last_error=self._last_error,
            recoverable_error_count=self.recoverable_error_count,
            captured_at=datetime.now(UTC),
            address=self.address,
            protocol=self.protocol,
            type_code=info.type_code.raw if info is not None else None,
            capabilities=_supported(self._availability),
            availability=self.availability,
            channels=tuple(c.channel for c in self._channels),
        )

    # --- The choke point -----------------------------------------------------------------

    def gate(
        self,
        operation: str,
        *,
        tier: SafetyTier = SafetyTier.READ_ONLY,
        confirm: bool = False,
        requires: Capability = Capability.NONE,
        subject: str | None = None,
    ) -> None:
        """Walk the gates of the module docstring; nothing is sent either way.

        ``subject`` names what the operation acts on, for the refusal's message.

        Raises:
            FujiConnectionError: the session is closed or broken.
            FujiConfirmationRequiredError: ``tier`` is above ``READ_ONLY`` and
                ``confirm`` is not ``True``.
            FujiCapabilityError: a capability in ``requires`` is known to be
                unsupported, or an option in it is not taken as fitted.
        """
        self._check_state(operation)
        if tier > SafetyTier.READ_ONLY and confirm is not True:
            what = subject or operation
            msg = f"{what} is {tier.name}; pass confirm=True to go ahead"
            raise FujiConfirmationRequiredError(
                msg, context=self._context(operation).merged(safety=tier.name.lower())
            )
        self._check_capability(requires, operation)
        self._check_options(requires, operation)

    async def run[T](
        self,
        operation: str,
        body: Callable[[ProtocolClient, Deadline], Awaitable[T]],
        *,
        timeout: float | None = None,
        requires: Capability = Capability.NONE,
        tier: SafetyTier = SafetyTier.READ_ONLY,
        confirm: bool = False,
    ) -> T:
        """Run ``body`` as one operation, after the gates (see the module docstring).

        ``body`` receives the station's client and the operation's deadline,
        which it passes to every read so the budget is shared.

        Raises:
            FujiConnectionError: the session is closed or broken; nothing was sent.
            FujiConfirmationRequiredError: ``tier`` needs ``confirm=True``;
                nothing was sent.
            FujiCapabilityError: a capability in ``requires`` is known to be
                unsupported, or an option in it is not fitted; nothing was sent.
            FujiValidationError: ``timeout`` is negative or not finite.
            FujiError: whatever ``body`` raised, with the port and station in its context.
        """
        self.gate(operation, tier=tier, confirm=confirm, requires=requires)
        deadline = Deadline.after(timeout, operation=operation)
        refused: FujiConnectionError | None = None
        try:
            with deadline.enforce():
                while True:
                    port = self._port
                    async with maybe_acquire(port.lock):
                        if port is not self._port:
                            continue  # reopened while this call waited: take the new port's lock
                        # The session may have been closed or broken while this call
                        # waited for the lock. A refusal is not a failure of the
                        # analyzer, so it is raised below, not kept as the last error.
                        refused = self._state_error(operation)
                        if refused is None:
                            return await body(self._client, deadline)
                        break
        except FujiError as exc:
            located = self._located(exc, operation)
            self._note_failure(located)
            if located is exc:
                raise
            raise located from exc.__cause__
        assert refused is not None  # noqa: S101 - the body returned otherwise
        raise refused

    def _check_state(self, operation: str) -> None:
        refused = self._state_error(operation)
        if refused is not None:
            raise refused

    def _state_error(self, operation: str) -> FujiConnectionError | None:
        if self._state is SessionState.OPEN:
            return None
        if self._state is SessionState.BROKEN:
            msg = f"the connection to {self.port} failed; open the analyzer again"
        else:
            msg = "the analyzer is closed"
        return FujiConnectionError(msg, context=self._context(operation))

    def _check_capability(self, requires: Capability, operation: str) -> None:
        for capability in PROBED_CAPABILITIES:
            if (
                capability in requires
                and self._availability[capability] is Availability.UNSUPPORTED
            ):
                raise self._unsupported(capability, operation)

    def _check_options(self, requires: Capability, operation: str) -> None:
        wanted = requires & OPTION_CAPABILITIES
        if not wanted:
            return
        info = self._info
        for capability in wanted:
            name = _option_name(capability)
            possible = MODEL_OPTIONS.get(info.model) if info is not None else None
            if info is not None and possible is not None and capability not in possible:
                msg = f"the {info.model} has no {name}: its manual describes no such option"
                raise FujiCapabilityError(msg, context=self._context(operation))
            if info is None:
                msg = (
                    f"the model, and so whether it can have the {name} option, is not "
                    "known before identify(); identify the analyzer first"
                )
                raise FujiCapabilityError(msg, context=self._context(operation))
            if capability in self.options:
                continue
            if info.type_code.options is None:
                msg = (
                    f"the type code {info.type_code.raw!r} does not say whether the {name} "
                    "option is fitted; assert it with options= if it is"
                )
            else:
                msg = (
                    f"the type code {info.type_code.raw!r} does not list the {name} option; "
                    "assert it with options= if it is fitted"
                )
            raise FujiCapabilityError(msg, context=self._context(operation))

    def _unsupported(self, capability: Capability, operation: str) -> FujiCapabilityError:
        name = (capability.name or str(capability)).lower()
        cls = FujiFirmwareError if capability in _FIRMWARE_CAPABILITIES else FujiCapabilityError
        msg = f"this analyzer does not have the {name} capability (its probe was refused)"
        return cls(msg, context=self._context(operation))

    def _context(self, operation: str) -> ErrorContext:
        return ErrorContext(
            command_name=operation, protocol=self.protocol, port=self.port, address=self.address
        )

    def _located(self, exc: FujiError, operation: str) -> FujiError:
        context = exc.context
        if context.port is not None and context.command_name is not None:
            return exc
        return exc.with_context(
            command_name=context.command_name or operation,
            protocol=context.protocol or self.protocol,
            port=context.port or self.port,
            address=context.address if context.address is not None else self.address,
        )

    def note_port_failure(self, error: FujiError) -> None:
        """Take in a failure reported in a result rather than raised.

        A connection failure breaks the session, as a raised one does.
        """
        self._note_failure(error)

    def _note_failure(self, error: FujiError) -> None:
        self._last_error = error.context
        port_failed = isinstance(error, FujiConnectionError) or (
            isinstance(error, FujiWriteOutcomeUnknownError)
            and error.context.extra.get("failure") == FailureKind.CONNECTION.value
        )
        if port_failed and self._state is SessionState.OPEN:
            self._state = SessionState.BROKEN
            _LOG.warning("%s station %d: the connection failed: %s", self.port, self.address, error)

    # --- Capabilities --------------------------------------------------------------------

    async def read_capability[T](
        self, capability: Capability, operation: str, read: Callable[[], Awaitable[T]]
    ) -> T:
        """Run ``read`` of a probed capability, keeping its availability current.

        Exception 02 to a well-formed read marks the capability ``UNSUPPORTED``
        and raises :class:`FujiCapabilityError`; words that do not validate mark
        it ``INVALID_DATA``; a result marks it ``SUPPORTED``. Anything else, such
        as a timeout, says nothing about it.
        """
        try:
            result = await read()
        except FujiModbusIllegalDataAddressError as exc:
            self.set_availability(capability, Availability.UNSUPPORTED)
            raise self._unsupported(capability, operation) from exc
        except FujiDecodeError:
            self.set_availability(capability, Availability.INVALID_DATA)
            raise
        self.set_availability(capability, Availability.SUPPORTED)
        return result

    def set_availability(self, capability: Capability, availability: Availability) -> None:
        """Record what is now known about ``capability``."""
        self._availability[capability] = availability
        self._refresh_info()

    # --- Learning ------------------------------------------------------------------------

    def learn_identity(
        self, identity: Identity, *, channel_map: Mapping[ChannelId, Gas] | None = None
    ) -> DeviceInfo:
        """Take in an identity read; ``channel_map`` replaces the asserted map when given.

        Raises:
            FujiProtocolUnsupportedError: the type code does not name a ZP model.
        """
        asserted = self._asserted if channel_map is None else MappingProxyType(dict(channel_map))
        # A channel established by the map it replaces stays established (design §6.7).
        established = self._seen | set(self._asserted)
        info = describe_identity(
            identity,
            address=self.address,
            serial_settings=self.serial_settings,
            asserted=asserted,
            established=established,
        )
        self._asserted = asserted
        self._seen = established | identity.nonzero
        self._availability.update(identity.availability)
        self._ranges, self._ranges_stale = identity.ranges, False
        self._current_ranges = identity.current_ranges
        self._info = info
        self._relabel()
        return info

    def learn_ranges(self, ranges: tuple[RangeInfo, ...]) -> tuple[RangeInfo, ...]:
        """Take in a fresh read of the range tables."""
        self._ranges, self._ranges_stale = ranges, False
        self._refresh_info()
        return ranges

    def learn_current_ranges(self, current: Mapping[ChannelId, int]) -> None:
        """Take in the current range of channels 1-5; a change makes the range tables stale."""
        previous = self._current_ranges
        if previous is not None and dict(previous) != dict(current) and self._ranges is not None:
            _LOG.info(
                "%s station %d: the current range changed; the range tables will be read again",
                self.port,
                self.address,
            )
            self._ranges_stale = True
        self._current_ranges = MappingProxyType(dict(current))

    def learn_poll(self, poll: PollRead) -> Frame:
        """Decode a poll, first establishing any channel it shows alive (design §2.9)."""
        fresh = poll.nonzero - self._seen - set(self._asserted)
        self._seen |= poll.nonzero
        if fresh:
            _LOG.info(
                "%s station %d: %s read non-zero and joined the established channels",
                self.port,
                self.address,
                ", ".join(c.value for c in sorted(fresh, key=lambda c: c.number)),
            )
            self._relabel()
        self.learn_current_ranges(poll.current_ranges)
        frame = poll.decode(self._channels)
        self._last_frame = frame
        return frame

    async def ensure_ranges(
        self, client: ProtocolClient, deadline: Deadline
    ) -> tuple[RangeInfo, ...]:
        """The range tables, read again first when none are cached or they are stale."""
        if self._ranges is None or self._ranges_stale:
            return self.learn_ranges(await read_ranges(client, deadline=deadline))
        return self._ranges

    # --- Writes --------------------------------------------------------------------------

    async def check_quiet(
        self, client: ProtocolClient, deadline: Deadline, operation: str
    ) -> StatusRead:
        """Read the status, and refuse a write or command it forbids (design §6.1).

        Raises:
            FujiAnalyzerStateError: a calibration is running or the front panel
                is in a menu; nothing was written.
        """
        status = await read_status(client, deadline=deadline)
        self.learn_current_ranges({c: s.range for c, s in status.channels.items()})
        reasons = busy_reasons(status)
        if reasons:
            msg = f"{operation} refused, nothing was written: {'; '.join(reasons)}"
            raise FujiAnalyzerStateError(
                msg, context=self._context(operation).merged(reasons=tuple(reasons))
            )
        return status

    async def write_setting(
        self,
        client: ProtocolClient,
        deadline: Deadline,
        prepared: PreparedValue,
        *,
        command: str,
    ) -> WriteResult:
        """Write one prepared setting and read it back, under the operation lock.

        In order: the status (refusing while a calibration runs or the panel is
        in a menu), the range tables for a range-scaled value, the setting and
        what it depends on, the encoding against that range, then the write and
        its read-back (:func:`~fujilib.devices.writes.write_setting`). A
        selected range that reads back as written is then waited for until the
        channel measures on it. The range tables are read again next time they
        are needed after a range or range-scaled write (design §6.7).

        Raises:
            FujiAnalyzerStateError: the status forbids a write now.
            FujiValidationError: the value does not fit the range, or a setting
                it depends on does not allow it; nothing was written.
            FujiVerificationError: a selected range reads back as written, but
                the channel did not measure on it within the read-back budget.
            FujiError: as :func:`~fujilib.devices.writes.write_setting`.
        """
        spec = prepared.spec
        await self.check_quiet(client, deadline, command)
        requirement = _REQUIRES_SETTING.get(spec.name)
        scaled = spec.scaling.kind is ScalingKind.BY_RANGE
        ranges: tuple[RangeInfo, ...] = ()
        range_info = None
        if scaled or requirement is not None:
            ranges = self.learn_ranges(await read_ranges(client, deadline=deadline))
        if scaled:
            range_info = _range_of(ranges, spec, spec.range or 0)
        names = (spec.name,) if requirement is None else (spec.name, requirement[0])
        current = await read_registers(client, names, ranges=ranges, deadline=deadline)
        if requirement is not None:
            _check_requirement(spec, current[requirement[0]], requirement[1])
            assert isinstance(prepared.value, RangeIndex)  # noqa: S101 - its encoding says so
            _range_of(ranges, spec, prepared.value.number)
        raw = encode_prepared(prepared, range_info)
        self._write_rate.record(spec.name, where=f"{self.port} station {self.address}")
        try:
            result = await write_setting(
                client,
                spec,
                raw,
                previous=current[spec.name],
                scaling=(range_info[2], range_info[0]) if range_info is not None else None,
                deadline=deadline,
                verify_timeout=self._verify_timeout,
                command=command,
            )
        finally:
            if spec.name.startswith("range.") or range_info is not None:
                self._ranges_stale = True
        if result.verified and spec.name in _CURRENT_RANGE:
            await self._await_range(client, result, command)
        return result

    async def _await_range(self, client: ProtocolClient, result: WriteResult, command: str) -> None:
        """Wait until the channel measures on the range ``result`` selected.

        The reads run shielded, within the read-back budget, as the read-back
        does, so a write that used up the operation's deadline is still followed.

        Raises:
            FujiVerificationError: the channel still measured on another range
                when the budget ran out, or its current range could not be read.
            FujiConnectionError: the port failed.
        """
        name = _CURRENT_RANGE[result.name]
        seen: RegisterValue | None = None
        failure: FujiError | None = None
        deadline = Deadline.after(self._verify_timeout, operation=f"{command} range")
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    seen = (await read_registers(client, (name,), deadline=deadline))[name]
                except FujiConnectionError:
                    raise
                except FujiError as exc:
                    failure = exc
                    break
                if seen.words == result.requested.words:
                    return
                if deadline.remaining() <= _RANGE_POLL_S:
                    break
                await anyio.sleep(_RANGE_POLL_S)
        channel = result.requested.spec.channel
        assert channel is not None  # noqa: S101 - range settings belong to a channel
        wrote = f"{result.name}: wrote {describe(result.requested)} and it reads back"
        if seen is None:
            msg = f"{wrote}, but {channel.value}'s current range could not be read: {failure}"
        else:
            msg = (
                f"{wrote}, but {channel.value} still measures on {describe(seen)} "
                f"after {deadline.elapsed():.2f} s"
            )
        context = ErrorContext(
            command_name=command,
            extra={
                "setting": result.name,
                "write_state": result.state.value,
                "requested_raw": result.requested.raw,
                "current_range_raw": seen.raw if seen is not None else None,
            },
        )
        raise FujiVerificationError(msg, context=context) from failure

    def _relabel(self) -> None:
        type_code = self._info.type_code if self._info is not None else None
        self._channels = label_channels(self._seen, asserted=self._asserted, type_code=type_code)
        for info in self._channels:
            suggestion = info.suggested_gas
            if suggestion is None or info.channel not in self._asserted or suggestion is info.gas:
                continue
            key = (info.channel, info.gas, suggestion)
            if key not in self._warned:
                self._warned.add(key)
                _LOG.warning(
                    "%s station %d: %s is asserted as %s, but the type code suggests %s",
                    self.port,
                    self.address,
                    info.channel.value,
                    info.gas.value,
                    suggestion.value,
                )
        self._refresh_info()

    def _refresh_info(self) -> None:
        info = self._info
        if info is None:
            return
        availability = MappingProxyType(dict(self._availability))
        self._info = replace(
            info,
            channels=self._channels,
            ranges=self._ranges if self._ranges is not None else info.ranges,
            capabilities=_supported(availability),
            availability=availability,
            health=_health(info.type_code, availability),
        )

    # --- Lifecycle -----------------------------------------------------------------------

    async def reopen(self, *, timeout: float | None = None) -> DeviceInfo:
        """Open the port again and identify the analyzer, usually after a connection failure.

        The old port is closed first (after the operation in progress), then
        opened again with the settings it was first opened with. The station
        must identify as the analyzer that was open: the same serial number and
        type code. The asserted channel map, the established channels and the
        traffic counters are kept. Calls made meanwhile are refused. Reopens
        are taken one at a time, and a call that finds the analyzer reopened
        by another while it waited returns at once. :meth:`close` waits for a
        reopen in progress, so no port is left open once it returns.

        Raises:
            FujiConfigurationError: the analyzer was closed; its port came from
                the caller, so it cannot be reopened; or another analyzer answers
                on the station. None of these changes by trying again.
            FujiConnectionError: the port cannot be opened.
            FujiValidationError: ``timeout`` is negative or not finite.
            FujiError: identification failed. The session stays broken.
        """
        operation = "reopen"
        reopener = self._reopener
        if reopener is None:
            msg = (
                f"the analyzer on {self.port} was opened on a transport the caller supplied, "
                "so fujilib cannot open it again"
            )
            raise FujiConfigurationError(msg, context=self._context(operation))
        deadline = Deadline.after(timeout, operation=operation)
        found = self._port
        try:
            with deadline.enforce():
                async with self._reopening:
                    self._check_not_closed(operation)
                    if self._port is not found and self._state is SessionState.OPEN:
                        return self._require_info()  # another call reopened it meanwhile
                    self._state = SessionState.BROKEN
                    await self._port.aclose()
                    port = await reopener()
                    try:
                        self._check_not_closed(operation)
                        client, identity = await self._identify_on(port, deadline)
                        self._check_not_closed(operation)
                    except BaseException:
                        with anyio.CancelScope(shield=True):
                            await port.aclose()
                        raise
                    # The session's counters go on, with the new port's traffic so far.
                    client.counters = _added(self._client.counters, client.counters)
                    self._port, self._client = port, client
                    self._state = SessionState.OPEN
        except FujiError as exc:
            located = self._located(exc, operation)
            self._last_error = located.context
            if located is exc:
                raise
            raise located from exc.__cause__
        _LOG.info("%s station %d: reopened", self.port, self.address)
        return self.learn_identity(identity)

    def _check_not_closed(self, operation: str) -> None:
        if self._state is SessionState.CLOSED:
            msg = "the analyzer was closed; open it again with open_device"
            raise FujiConfigurationError(msg, context=self._context(operation))

    def _require_info(self) -> DeviceInfo:
        info = self._info
        assert info is not None  # noqa: S101 - a reopen that succeeded identified it
        return info

    async def _identify_on(
        self, port: ModbusPort, deadline: Deadline
    ) -> tuple[ModbusClient, Identity]:
        """Identify the station on a newly opened ``port``, checking it is the same analyzer."""
        client = port.client(self.address)
        async with maybe_acquire(port.lock):
            identity = await self._profile.identify(client, probe=True, deadline=deadline)
        info = self._info
        if info is not None and (
            identity.serial_number != info.serial_number
            or identity.type_code.raw != info.type_code.raw
        ):
            msg = (
                f"{port.label} station {self.address} now answers as "
                f"{identity.type_code.raw!r} serial {identity.serial_number!r}, not the "
                f"analyzer that was open ({info.type_code.raw!r} serial {info.serial_number!r})"
            )
            raise FujiConfigurationError(msg, context=self._context("reopen"))
        return client, identity

    async def close(self) -> None:
        """Close the session and its port. Idempotent.

        Waits for an operation in progress to finish, and completes even when
        the caller is cancelled (the port closes shielded). A transport the
        caller supplied is left open.
        """
        self._state = SessionState.CLOSED
        # Every call waits: the port closes once, after the operation (or the
        # reopen) in progress.
        with anyio.CancelScope(shield=True):
            async with self._reopening:
                await self._port.aclose()

    def __repr__(self) -> str:
        return f"<Session {self.port} station {self.address} {self._state.value}>"


def _read_budget(port: ModbusPort, *, blocks: int) -> float:
    """Seconds ``blocks`` reads may take on ``port``, with every retry and late-reply window."""
    return blocks * (port.resync_window + (port.read_retries + 1) * port.request_timeout)


def _range_of(
    ranges: Iterable[RangeInfo], spec: RegisterSpec, number: int
) -> tuple[Unit, float, int]:
    """``(unit, full scale, decimals)`` of range ``number`` of ``spec``'s channel.

    The range tables list two ranges for every channel, so a channel's range
    count decides which exist.

    Raises:
        FujiValidationError: the channel has no such range.
    """
    assert spec.channel is not None  # noqa: S101 - range settings belong to a channel
    info = {i.channel: i for i in ranges}[spec.channel]
    if not 1 <= number <= info.count:
        msg = f"{spec.name}: {spec.channel.value} has {info.count} range(s), so no range {number}"
        raise FujiValidationError(msg, context=ErrorContext(extra={"setting": spec.name}))
    return info.of(number)


def _check_requirement(spec: RegisterSpec, current: RegisterValue, needed: RangeMethod) -> None:
    if current.value is needed:
        return
    shown = current.value.name.lower() if isinstance(current.value, RangeMethod) else current.raw
    msg = (
        f"{spec.name} is used only while {current.spec.name} is {needed.name.lower()}, "
        f"and it is {shown}; write {current.spec.name} first"
    )
    raise FujiValidationError(msg, context=ErrorContext(extra={"setting": spec.name}))


def _added(kept: ClientCounters, more: ClientCounters) -> ClientCounters:
    """``kept``, the object callers hold, with ``more`` added to it."""
    kept.requests += more.requests
    kept.retries += more.retries
    kept.recovered += more.recovered
    for kind, count in more.failures.items():
        kept.failures[kind] = kept.failures.get(kind, 0) + count
    return kept
