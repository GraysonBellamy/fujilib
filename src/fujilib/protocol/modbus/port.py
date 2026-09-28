"""One Modbus bus per serial port (design §4.2).

:class:`ModbusPort` owns the single ``anymodbus.Bus`` of a port and hands out
one :class:`~fujilib.protocol.modbus.client.ModbusClient` per station. It
never exposes a raw ``anymodbus.Slave``: only the client holds one (design
§5.4). It keeps three pieces of timing state that ``anymodbus`` cannot keep
for fujilib:

- **When the last transaction ended.** Clients wait out the inter-frame gap
  themselves before stamping a request's time, so a
  :class:`~fujilib.devices.models.TransferTiming` starts when the request goes
  out rather than before ``anymodbus``'s own wait for the gap. ``anymodbus``
  then finds the gap already elapsed and sends at once.
- **The one-shot startup settle**, for the same reason.
- **A quiet window after an uncertain transaction.** A cancelled, timed-out,
  garbled or mismatched transaction may leave its reply on the wire.
  ``anymodbus`` clears the input buffer before every request, but that only
  drops bytes that have already arrived. A reply to an FC03/04 read carries no
  address, so a late one could be accepted as the answer to the next read of
  the same length. No request therefore goes out until the window has passed
  and the late bytes, if any, have landed and can be cleared.

**Two locks.** ``anymodbus``'s internal lock serializes *transactions*.
:attr:`ModbusPort.lock` serializes *operations*: sequences such as the two
blocks of a poll that must not interleave with other traffic on the port. It
is taken with :func:`fujilib._lock.maybe_acquire`, so a caller can hold it
across a batch.

**Ownership.** A transport carries at most one open port; opening a second
one on it is refused. The port closes the transport on :meth:`ModbusPort.aclose`
only when it was created owning it, so a caller's transport is never closed
from under them.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final, Self
from weakref import WeakValueDictionary

import anyio
from anymodbus import Bus, BusConfig, RetryPolicy, TimingConfig

from fujilib._lock import maybe_acquire
from fujilib.config import DEFAULTS
from fujilib.errors import (
    ErrorContext,
    FujiConfigurationError,
    FujiConnectionError,
    FujiResyncRequiredError,
    FujiValidationError,
)
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.client import ModbusClient

if TYPE_CHECKING:
    from types import TracebackType

    from fujilib._deadline import Deadline
    from fujilib.transport.base import Transport

__all__ = ["MAX_STATION", "MIN_STATION", "ModbusPort"]

#: Station numbers the analyzer accepts; 0 disables its communication (design §2.1).
MIN_STATION: Final = 1
MAX_STATION: Final = 31

#: ``anymodbus`` accepts request timeouts in (0, 60] seconds.
_MAX_REQUEST_TIMEOUT: Final = 60.0

# Open ports by the id of their transport. A port keeps its transport alive,
# so the id cannot be reused while the entry exists; a port that is dropped
# without being closed disappears from here with it.
_CLAIMS: WeakValueDictionary[int, ModbusPort] = WeakValueDictionary()


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_seconds(name: str, value: float, *, positive: bool = False) -> float:
    ok = math.isfinite(value) and (value > 0 if positive else value >= 0)
    if not ok:
        kind = "positive" if positive else "non-negative"
        msg = f"{name} must be a finite, {kind} number of seconds, got {value!r}"
        raise FujiValidationError(msg)
    return float(value)


class ModbusPort:
    """The Modbus side of one serial port. Internal; the facade builds it."""

    def __init__(
        self,
        transport: Transport,
        *,
        request_timeout: float = DEFAULTS.request_timeout_s,
        inter_frame_idle: float = DEFAULTS.inter_frame_idle_s,
        startup_settle: float = DEFAULTS.startup_settle_s,
        read_retries: int = DEFAULTS.read_retries,
        resync_window: float = DEFAULTS.resync_window_s,
        owns_transport: bool = False,
    ) -> None:
        """Bind a bus to ``transport.stream``.

        Args:
            transport: The open transport. It must not already carry an open port.
            request_timeout: Seconds to wait for each reply.
            inter_frame_idle: Idle seconds before each request, from the end
                of the previous transaction.
            startup_settle: One-shot idle seconds before the first request.
            read_retries: Extra attempts after a read fails in transit.
            resync_window: Quiet seconds after an uncertain transaction.
            owns_transport: Close ``transport`` when the port is closed.

        Raises:
            FujiValidationError: a timing value or retry count is out of range.
            FujiConfigurationError: ``transport`` already carries an open port.
        """
        self._request_timeout = _check_seconds("request_timeout", request_timeout, positive=True)
        if self._request_timeout > _MAX_REQUEST_TIMEOUT:
            msg = (
                f"request_timeout must be at most {_MAX_REQUEST_TIMEOUT:g} s, got {request_timeout}"
            )
            raise FujiValidationError(msg)
        self._inter_frame_idle = _check_seconds("inter_frame_idle", inter_frame_idle)
        self._startup_settle = _check_seconds("startup_settle", startup_settle)
        self._resync_window = _check_seconds("resync_window", resync_window)
        if not _is_count(read_retries):
            msg = f"read_retries must be a non-negative integer, got {read_retries!r}"
            raise FujiValidationError(msg)
        self._read_retries = read_retries

        existing = _CLAIMS.get(id(transport))
        if existing is not None and not existing.closed:
            msg = f"{transport.label} already has an open Modbus port"
            raise FujiConfigurationError(msg, context=ErrorContext(port=transport.label))

        self._transport = transport
        self._owns_transport = owns_transport
        # anymodbus retries nothing (the client retries and counts, design §4.5)
        # and settles nothing (the port settles before stamping the first request).
        self._bus = Bus(
            transport.stream,
            config=BusConfig(
                request_timeout=self._request_timeout,
                retries=RetryPolicy(retries=0),
                timing=TimingConfig(inter_frame_idle=self._inter_frame_idle, startup_settle=0.0),
            ),
        )
        self._lock = anyio.Lock()
        self._clients: dict[int, ModbusClient] = {}
        self._last_end: float | None = None
        self._settled = False
        self._quiet_until = -math.inf
        self._closed = False
        # Claimed last, so a port that failed to build never holds the transport.
        _CLAIMS[id(transport)] = self

    # --- Properties ------------------------------------------------------------------------

    @property
    def transport(self) -> Transport:
        """The transport the bus is bound to."""
        return self._transport

    @property
    def label(self) -> str:
        """The transport's canonical port name."""
        return self._transport.label

    @property
    def protocol(self) -> ProtocolKind:
        """Always :attr:`ProtocolKind.MODBUS_RTU`."""
        return ProtocolKind.MODBUS_RTU

    @property
    def lock(self) -> anyio.Lock:
        """The operation lock; take it with :func:`fujilib._lock.maybe_acquire`."""
        return self._lock

    @property
    def closed(self) -> bool:
        """Whether :meth:`aclose` has been called."""
        return self._closed

    @property
    def request_timeout(self) -> float:
        """Seconds to wait for each reply."""
        return self._request_timeout

    @property
    def inter_frame_idle(self) -> float:
        """Idle seconds before each request."""
        return self._inter_frame_idle

    @property
    def read_retries(self) -> int:
        """Extra attempts after a read fails in transit."""
        return self._read_retries

    @property
    def resync_window(self) -> float:
        """Quiet seconds after an uncertain transaction."""
        return self._resync_window

    def quiet_remaining(self) -> float:
        """Seconds until the quiet window after an uncertain transaction ends; 0 if none."""
        return max(0.0, self._quiet_until - anyio.current_time())

    # --- Stations --------------------------------------------------------------------------

    def client(self, address: int) -> ModbusClient:
        """The client for station ``address``, created on first use.

        Raises:
            FujiValidationError: ``address`` is not a station number, 1-31.
        """
        if not _is_count(address):
            msg = f"station address must be an integer, got {address!r}"
            raise FujiValidationError(msg, context=ErrorContext(port=self.label))
        if not MIN_STATION <= address <= MAX_STATION:
            msg = f"station address must be {MIN_STATION}-{MAX_STATION}, got {address}"
            raise FujiValidationError(msg, context=ErrorContext(port=self.label, address=address))
        client = self._clients.get(address)
        if client is None:
            client = ModbusClient(self, address, self._bus.slave(address))
            self._clients[address] = client
        return client

    # --- Timing (called by clients under the operation lock) -------------------------------

    async def ready(self, deadline: Deadline, *, context: ErrorContext) -> None:
        """Wait until a request may be sent.

        Waits out the startup settle, the inter-frame gap and any quiet window.

        Raises:
            FujiConnectionError: the port or its transport is closed; nothing was sent.
            FujiResyncRequiredError: ``deadline`` ends before the quiet window
                does; nothing was sent.
        """
        if self._closed:
            msg = f"the Modbus port on {self.label} is closed"
            raise FujiConnectionError(msg, context=context)
        if not self._transport.is_open:
            # Checked here, before anything is sent, so a request on a closed
            # port is a definite failure rather than a write of unknown outcome.
            msg = f"the transport of {self.label} is closed"
            raise FujiConnectionError(msg, context=context)
        if self._quiet_until > deadline.expires:
            msg = (
                f"{self.label} is waiting out a possible late reply for "
                f"{self.quiet_remaining():.3f} s, longer than the time left"
            )
            raise FujiResyncRequiredError(msg, context=context)
        now = anyio.current_time()
        if self._last_end is not None:
            at = self._last_end + self._inter_frame_idle
        elif not self._settled:
            self._settled = True
            at = now + self._startup_settle
        else:
            at = now
        at = max(at, self._quiet_until)
        if at > now:
            await anyio.sleep_until(at)

    def transaction_ended(self, *, certain: bool) -> None:
        """Record that a transaction ended; start a quiet window unless its outcome is certain.

        An outcome is certain when a well-formed reply of the expected length,
        or an exception reply, was received.
        """
        now = anyio.current_time()
        self._last_end = now
        if not certain:
            self._quiet_until = max(self._quiet_until, now + self._resync_window)

    # --- Lifecycle -------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the port, and its transport if the port owns it. Idempotent.

        Waits for an operation in progress to finish (each transaction is bounded
        by the request timeout), so no request of this port is still waiting for
        its reply when the transport is released or closed. Completes even when
        the caller is cancelled.
        """
        if self._closed:
            return
        with anyio.CancelScope(shield=True):
            async with maybe_acquire(self._lock):
                await self._close_locked()

    async def _close_locked(self) -> None:
        if self._closed:  # another task closed it while this one waited for the lock
            return
        self._closed = True
        # While this port is open the claim can only be its own (see __init__).
        _CLAIMS.pop(id(self._transport), None)
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<ModbusPort {self.label} {state}>"
