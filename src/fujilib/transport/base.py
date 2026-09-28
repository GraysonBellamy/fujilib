"""The transport contract and serial framing settings (design §2.1, §4.1).

The ZP series' serial settings are fixed and cannot be changed:
**38400 bps, 8 data bits, no parity, 1 stop bit, no flow control**. They are
still carried as a value so a session can report what it actually used and a
second open with incompatible settings can be refused.

A :class:`Transport` is a thin lifecycle object that **exposes** its byte
stream instead of being one. ``anymodbus`` keeps its drain-after-send and
input-reset behaviour only when the stream it is given is literally an
``anyserial.SerialPort``, so the Modbus port binds its bus to
:attr:`Transport.stream` directly (design §4.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from anyserial import ByteSize, Parity, StopBits

if TYPE_CHECKING:
    import anyio.abc

__all__ = ["FUJI_BAUDRATE", "SerialSettings", "Transport"]

#: The only baud rate the ZP series supports.
FUJI_BAUDRATE: Final = 38_400


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """Frozen serial framing descriptor. The defaults are the analyzer's fixed 38400 8-N-1."""

    port: str
    baudrate: int = FUJI_BAUDRATE
    bytesize: ByteSize = ByteSize.EIGHT
    parity: Parity = Parity.NONE
    stopbits: StopBits = StopBits.ONE
    rtscts: bool = False
    xonxoff: bool = False
    exclusive: bool = True


@runtime_checkable
class Transport(Protocol):
    """An open connection to one serial line.

    Implementations: :class:`~fujilib.transport.serial.SerialTransport` over a
    real port (or one end of a test pair), and
    :class:`~fujilib.transport.fake.FakeTransport` for byte-exact fixture replay.
    """

    @property
    def label(self) -> str:
        """The canonical port name, used in error context and as the ownership key."""
        ...

    @property
    def is_open(self) -> bool:
        """Whether the transport is open."""
        ...

    @property
    def settings(self) -> SerialSettings:
        """The serial settings in use."""
        ...

    @property
    def stream(self) -> anyio.abc.ByteStream:
        """The byte stream the Modbus bus binds to; a real ``SerialPort`` where there is one."""
        ...

    async def aclose(self) -> None:
        """Close the transport. Idempotent."""
        ...
