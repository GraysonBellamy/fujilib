"""Library defaults, each with the evidence it rests on (design §2.4, §4.2, §4.5).

The values come from the MODBUS manual (INZ-TN5A1190a-E) and from the
read-only bench measurements in ``docs/protocol-findings.md``. They are read
once, at import, and can be overridden per port (see
:class:`~fujilib.protocol.modbus.port.ModbusPort`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["DEFAULTS", "Defaults"]


@dataclass(frozen=True, slots=True)
class Defaults:
    """Library-wide default values."""

    #: Per-transaction reply timeout, in seconds. The bench round trip through
    #: an FTDI adapter at its default 16 ms latency timer is 12-27 ms for one
    #: word and 50-63 ms for 64 words (findings §6.3); the manual allows a
    #: slave turnaround of up to 30 ms. 0.5 s leaves wide headroom.
    request_timeout_s: float = 0.5

    #: Idle time before each request, measured from the end of the previous
    #: transaction however it ended. The manual needs at least 2.5 ms and
    #: recommends 5 ms (TN5A1190a p.17-18). The bench unit needs at most 1 ms
    #: after any reply (findings §6.3). On Windows any sleep this short lasts
    #: about 16 ms, which is harmless at 1 Hz.
    inter_frame_idle_s: float = 0.005

    #: One-shot wait before the first request on a newly opened port, for the
    #: RS-485 adapter to settle. The bench probes ran with it (findings §4.3).
    startup_settle_s: float = 0.05

    #: Extra attempts after a read fails in transit (timeout, bad CRC, a
    #: malformed or mismatched reply). The manual asks the master "to provide
    #: 3 times or more retries" (TN5A1190a p.17). The bench lost about one
    #: request in 3,000, with no pattern (findings §6.3), so two retries (three
    #: attempts) already make a failed read vanishingly rare. Writes are never
    #: retried (design §4.5).
    read_retries: int = 2

    #: After a transaction whose reply may still be on the wire (cancelled,
    #: timed out, garbled or mismatched), no request goes out until this long
    #: after it ended, so the late bytes land before the next request clears
    #: the input buffer (design §4.2). A reply ends at most about 81 ms after
    #: its request: 30 ms turnaround (TN5A1190a p.17-18), about 35 ms to send a
    #: 64-word reply at 38400 baud, and the FTDI adapter's 16 ms latency timer.
    resync_window_s: float = 0.1


#: The defaults in use.
DEFAULTS: Final = Defaults()
