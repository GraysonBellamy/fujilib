"""Shared helpers for the read-only bench probes.

**Read-only by construction.** :class:`ReadOnlyStation` is the only door to the wire
and exposes the four Modbus *read* function codes (01-04) and nothing else, so no
probe built on it can change analyzer state.
"""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anymodbus import (
    Bus,
    BusConfig,
    FrameTimeoutError,
    ModbusError,
    ModbusExceptionResponse,
    RetryPolicy,
    TimingConfig,
)
from anyserial import ByteSize, Parity, SerialConfig, StopBits, open_serial_port

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from anymodbus import Slave

#: Analyzer framing is fixed and cannot be changed (INZ-TN5A1190a-E §4.1).
BAUDRATE = 38_400
#: The analyzer accepts at most 64 words per message (INZ-TN5A1190a-E Table 5-3).
MAX_WORDS = 64
#: Modbus spec ceiling; only the over-size test asks for more than ``MAX_WORDS``.
SPEC_MAX_WORDS = 125
MAX_STATION = 31
PARITY = {"none": Parity.NONE, "even": Parity.EVEN, "odd": Parity.ODD}

FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
READ_FUNCTION_CODES = (FC_READ_COILS, FC_READ_DISCRETE, FC_READ_HOLDING, FC_READ_INPUT)


class ReadOnlyStation:
    """One station, reachable through read function codes only."""

    def __init__(self, slave: Slave) -> None:
        self._slave = slave

    async def read(self, fc: int, address: int, count: int) -> tuple[int, ...]:
        """Issue one read. Bit reads come back as 0/1 integers."""
        if fc not in READ_FUNCTION_CODES:
            msg = f"refusing function code 0x{fc:02X}: probes are read-only"
            raise ValueError(msg)
        if not 1 <= count <= SPEC_MAX_WORDS:
            msg = f"count must be 1..{SPEC_MAX_WORDS}, got {count}"
            raise ValueError(msg)
        if fc == FC_READ_INPUT:
            return await self._slave.read_input_registers(address, count=count)
        if fc == FC_READ_HOLDING:
            return await self._slave.read_holding_registers(address, count=count)
        if fc == FC_READ_COILS:
            bits = await self._slave.read_coils(address, count=count)
        else:
            bits = await self._slave.read_discrete_inputs(address, count=count)
        return tuple(int(b) for b in bits)


@dataclass(frozen=True, slots=True)
class Attempt:
    """The outcome of one request."""

    fc: int
    address: int
    count: int
    outcome: str  # "ok" | "exception" | "silent" | "error"
    elapsed_ms: float
    words: tuple[int, ...] = ()
    exception_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def describe(self) -> str:
        head = f"fc{self.fc:02X} {self.address:04X}h x{self.count:<3d}"
        if self.outcome == "ok":
            tail = f"ok, {len(self.words)} word(s)"
        elif self.outcome == "exception":
            tail = f"exception {self.exception_code:02X}h"
        else:
            tail = f"{self.outcome} {self.detail}".rstrip()
        return f"{head} {self.elapsed_ms:7.1f} ms  {tail}"


async def attempt(station: ReadOnlyStation, fc: int, address: int, count: int) -> Attempt:
    """Issue one read and classify what came back. Never raises for a bus outcome."""
    start = time.perf_counter()
    try:
        words = await station.read(fc, address, count)
    except FrameTimeoutError:
        return Attempt(fc, address, count, "silent", _ms(start))
    except ModbusExceptionResponse as exc:
        return Attempt(
            fc, address, count, "exception", _ms(start), exception_code=int(exc.exception_code)
        )
    except ModbusError as exc:
        return Attempt(
            fc, address, count, "error", _ms(start), detail=f"{type(exc).__name__}: {exc}"
        )
    return Attempt(fc, address, count, "ok", _ms(start), words=tuple(words))


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def windows_port(port: str) -> str:
    r"""Return the ``\\.\COMn`` device path, which works for every COM number."""
    if sys.platform == "win32" and not port.startswith("\\\\.\\"):
        return "\\\\.\\" + port
    return port


def serial_config(baud: int = BAUDRATE, parity: str = "none") -> SerialConfig:
    return SerialConfig(
        baudrate=baud,
        byte_size=ByteSize.EIGHT,
        parity=PARITY[parity],
        stop_bits=StopBits.ONE,
    )


@asynccontextmanager
async def open_bus(
    port: str,
    *,
    baud: int = BAUDRATE,
    parity: str = "none",
    timeout: float = 0.3,
    idle: float = 0.005,
    retries: int = 0,
) -> AsyncGenerator[Bus]:
    """Open ``port`` and yield a bus. Closing the bus closes the port.

    The bus is handed the real ``anyserial`` port so ``anymodbus`` keeps its
    drain-after-send and input-flush behaviour (design §4.1).
    """
    stream = await open_serial_port(windows_port(port), serial_config(baud, parity))
    config = BusConfig(
        request_timeout=timeout,
        retries=RetryPolicy(retries=retries),
        timing=TimingConfig(inter_frame_idle=idle, startup_settle=0.05),
    )
    async with Bus(stream, config=config) as bus:
        yield bus


def parse_addresses(text: str) -> list[int]:
    """Parse ``"1"``, ``"1,3,5"`` or ``"1-31"`` into a list of station numbers."""
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    bad = [a for a in out if not 1 <= a <= MAX_STATION]
    if bad:
        msg = f"station numbers must be 1..{MAX_STATION}, got {bad}"
        raise ValueError(msg)
    return out


def words_to_text(words: tuple[int, ...]) -> str:
    """Render one-character-per-register words, low byte as ASCII."""
    return "".join(chr(w & 0xFF) if 0x20 <= (w & 0xFF) < 0x7F else "." for w in words)


def to_int16(word: int) -> int:
    """Interpret a register as a signed 16-bit value."""
    return word - 0x10000 if word & 0x8000 else word
