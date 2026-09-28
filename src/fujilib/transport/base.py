"""Serial framing settings (design §2.1, §4.1).

The ZP series' serial settings are fixed and cannot be changed:
**38400 bps, 8 data bits, no parity, 1 stop bit, no flow control**. They are
still carried as a value so a session can report what it actually used and a
second open with incompatible settings can be refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from anyserial import ByteSize, Parity, StopBits

__all__ = ["FUJI_BAUDRATE", "SerialSettings"]

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
