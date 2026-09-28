"""A scripted transport for byte-exact fixture replay (design §4.1, §10).

:class:`FakeTransport` is its own byte stream. Each request the client sends
is looked up in a script of exact request bytes; a match queues the scripted
reply, anything else is recorded and gets no reply. ``anymodbus`` sends each
request in a single ``send`` call, so the lookup sees whole frames.

It is used only to prove that the client puts the manual's exact bytes on the
wire. It is not a ``SerialPort``, so ``anymodbus`` skips drain and input
reset with it; those paths are covered by the simulated analyzer on a real
port pair (:mod:`fujilib.testing`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Self

import anyio
import anyio.abc
import anyio.lowlevel

from fujilib.transport.base import SerialSettings

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["FakeTransport"]


class FakeTransport(anyio.abc.ByteStream):
    """Replays scripted replies. Satisfies :class:`~fujilib.transport.base.Transport`."""

    def __init__(
        self,
        script: Mapping[bytes, bytes] | None = None,
        *,
        label: str = "fake://fixture",
    ) -> None:
        """Create a transport that answers each request in ``script`` with its reply.

        Args:
            script: Exact request bytes mapped to the reply bytes to send back.
            label: The name reported as the port.
        """
        self._script: Mapping[bytes, bytes] = MappingProxyType(dict(script or {}))
        self._settings = SerialSettings(port=label)
        self._inbound = bytearray()
        self._waiter: anyio.Event | None = None
        self._closed = False
        self.writes: list[bytes] = []
        """Every request sent, in order."""
        self.unmatched: list[bytes] = []
        """Requests the script had no reply for."""

    # --- Transport -----------------------------------------------------------------------

    @property
    def label(self) -> str:
        """The name reported as the port."""
        return self._settings.port

    @property
    def is_open(self) -> bool:
        """Whether :meth:`aclose` has not been called."""
        return not self._closed

    @property
    def settings(self) -> SerialSettings:
        """The default ZP settings, under :attr:`label`."""
        return self._settings

    @property
    def stream(self) -> Self:
        """The transport itself."""
        return self

    # --- ByteStream ----------------------------------------------------------------------

    async def send(self, item: bytes) -> None:
        """Record a request and queue its scripted reply, if it has one."""
        if self._closed:
            raise anyio.ClosedResourceError
        await anyio.lowlevel.checkpoint()
        request = bytes(item)
        self.writes.append(request)
        reply = self._script.get(request)
        if reply is None:
            self.unmatched.append(request)
            return
        self._inbound.extend(reply)
        self._wake()

    async def receive(self, max_bytes: int = 65536) -> bytes:
        """Return up to ``max_bytes`` queued reply bytes, waiting until there are some."""
        while not self._inbound:
            if self._closed:
                raise anyio.ClosedResourceError
            self._waiter = anyio.Event()
            await self._waiter.wait()
        chunk = bytes(self._inbound[:max_bytes])
        del self._inbound[:max_bytes]
        return chunk

    async def send_eof(self) -> None:
        """Nothing to do: the script decides what comes back."""

    async def aclose(self) -> None:
        """Close the transport and wake any pending receive. Idempotent."""
        self._closed = True
        self._wake()

    def _wake(self) -> None:
        if self._waiter is not None:
            self._waiter.set()
            self._waiter = None
