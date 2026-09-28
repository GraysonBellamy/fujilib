"""The session: the only path from the facade to the wire (design §6).

Every I/O call of an :class:`~fujilib.devices.analyzer.Analyzer` goes through
:meth:`Session.run`, which walks the gates before anything is sent:

1. **State.** The session is open, and no connection failure has broken it.
2. **Capability.** An operation that needs a probed capability is refused
   while that capability is known to be ``UNSUPPORTED`` (design §6.6).

The facade validates names, channels and arguments before it calls ``run``.
``run`` then starts the operation deadline, which also covers the wait for
the port's operation lock (design §6.4), and holds the lock for the whole
operation, so its transactions are never interleaved with other traffic on
the port. A failure is kept as :attr:`Session.last_error`. A connection
failure also breaks the session: every later call is refused until the
analyzer is opened again.

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
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib._deadline import Deadline
from fujilib._lock import maybe_acquire
from fujilib._logging import get_logger
from fujilib.devices.capability import PROBED_CAPABILITIES, Availability, Capability
from fujilib.devices.decode import label_channels
from fujilib.devices.models import DeviceHealth, DeviceInfo
from fujilib.devices.reads import read_ranges
from fujilib.devices.snapshot import FujiDeviceSnapshot
from fujilib.errors import (
    ErrorContext,
    FujiCapabilityError,
    FujiConnectionError,
    FujiDecodeError,
    FujiError,
    FujiFirmwareError,
    FujiModbusIllegalDataAddressError,
    FujiProtocolUnsupportedError,
)
from fujilib.protocol.base import ProtocolKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from fujilib.devices.models import ChannelInfo, Frame, RangeInfo
    from fujilib.devices.profile import DeviceProfile
    from fujilib.devices.reads import Identity, PollRead
    from fujilib.protocol.base import ProtocolClient
    from fujilib.protocol.modbus.client import ClientCounters
    from fujilib.protocol.modbus.port import ModbusPort
    from fujilib.registry.channels import ChannelId, Gas
    from fujilib.registry.typecode import TypeCode
    from fujilib.transport.base import SerialSettings

__all__ = ["Session", "SessionState", "describe_identity"]

_LOG = get_logger("session")

#: Capabilities that come with a firmware version rather than an option (design §6.6).
_FIRMWARE_CAPABILITIES: Final = frozenset({Capability.TYPE_CODE_EXT, Capability.CALIBRATION_LOG})


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
    ) -> None:
        """Bind to station ``address`` on ``port``, which the session then owns.

        Raises:
            FujiValidationError: ``address`` is not a station number, 1-31.
        """
        self._client = port.client(address)
        self._port = port
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
    def counters(self) -> ClientCounters:
        """The station's traffic counters: requests, retries and failures by kind."""
        return self._client.counters

    @property
    def recoverable_error_count(self) -> int:
        """Failed read attempts that a retry of the same read recovered (unified API §J)."""
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

    async def run[T](
        self,
        operation: str,
        body: Callable[[ProtocolClient, Deadline], Awaitable[T]],
        *,
        timeout: float | None = None,
        requires: Capability = Capability.NONE,
    ) -> T:
        """Run ``body`` as one operation, after the gates (see the module docstring).

        ``body`` receives the station's client and the operation's deadline,
        which it passes to every read so the budget is shared.

        Raises:
            FujiConnectionError: the session is closed or broken; nothing was sent.
            FujiCapabilityError: a capability in ``requires`` is known to be
                unsupported; nothing was sent.
            FujiValidationError: ``timeout`` is negative or not finite.
            FujiError: whatever ``body`` raised, with the port and station in its context.
        """
        self._check_state(operation)
        self._check_capability(requires, operation)
        deadline = Deadline.after(timeout, operation=operation)
        refused: FujiConnectionError | None = None
        try:
            with deadline.enforce():
                async with maybe_acquire(self._port.lock):
                    # The session may have been closed or broken while this call
                    # waited for the lock. A refusal is not a failure of the
                    # analyzer, so it is raised below, not kept as the last error.
                    refused = self._state_error(operation)
                    if refused is None:
                        return await body(self._client, deadline)
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

    def _note_failure(self, error: FujiError) -> None:
        self._last_error = error.context
        if isinstance(error, FujiConnectionError) and self._state is SessionState.OPEN:
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

    async def close(self) -> None:
        """Close the session and its port. Idempotent.

        Waits for an operation in progress to finish, and completes even when
        the caller is cancelled (the port closes shielded). A transport the
        caller supplied is left open.
        """
        self._state = SessionState.CLOSED
        # Every call waits: the port closes once, after the operation in progress.
        await self._port.aclose()

    def __repr__(self) -> str:
        return f"<Session {self.port} station {self.address} {self._state.value}>"
