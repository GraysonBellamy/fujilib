"""A simulated ZP-series analyzer and the RS-485 line it sits on (design §10).

Two pieces, kept apart so several stations can share one line:

- :class:`MockAnalyzer` is one station: its register banks, the region map it
  answers, the exception replies of one :class:`ExceptionProfile`, and the
  fault plan for its replies. It does no I/O.
- :class:`MockLine` is the line: ``anymodbus``'s ``MockServer`` on the
  analyzer end of a serial port pair. The server reads each request frame
  once and drops one with a bad CRC or for an absent station (the analyzer
  stays silent then). The line records the request and hands it to its
  station, whose reply goes out with the station's faults applied.

**Exception profiles.** Both follow the manual (TN5A1190a p.13): a read or
write that *starts* at an address the function cannot use answers 02, and one
whose count runs past the registers that exist, or asks for more than 64
words, answers 03. They differ where the bench unit (firmware 1.02) differs
from the manual: FC01 and FC02 answer 02 on the bench, 01 in the manual
(protocol findings §5). Function codes never sent to the bench answer as the
manual says in both.

**Writes.** The simulator accepts only the writes the manual documents minus
the ones fujilib must never make, **written out here independently of**
:data:`fujilib.registry.write_policy.WRITE_ENVELOPE`. A write outside that
list, above all to the key-simulation register 07D0h, raises
:class:`MockWriteViolation`, which fails the test. What the analyzer does
after an operation command is not simulated; a command is recorded.

The simulator validates library integration. It does not validate USB
timing, UART behaviour on real hardware, or analyzer semantics it was
programmed to assume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
import anyio.abc
from anymodbus.crc import crc16_modbus_bytes
from anymodbus.testing import MockServer, MockSlave

from fujilib.protocol.modbus.codec import encode_int
from fujilib.registry.channels import coerce_channel
from fujilib.registry.regions import (
    FC_READ_HOLDING,
    FC_READ_INPUT,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    RegisterTable,
)
from fujilib.registry.registers import REGISTRY
from fujilib.registry.units import Unit, unit_code

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from fujilib.registry.channels import ChannelId

__all__ = [
    "ExceptionProfile",
    "Fault",
    "FaultKind",
    "MockAnalyzer",
    "MockAnalyzerConfig",
    "MockExchange",
    "MockLine",
    "MockRegion",
    "MockRequest",
    "MockWriteViolation",
    "zp_readable_regions",
]

_FC_READ_COILS: Final = 0x01
_FC_READ_DISCRETE: Final = 0x02
_EXC_ILLEGAL_FUNCTION: Final = 0x01
_EXC_ILLEGAL_ADDRESS: Final = 0x02
_EXC_ILLEGAL_VALUE: Final = 0x03
_MAX_WORDS: Final = 64
_WORD_MAX: Final = 0xFFFF

# Function codes whose request body is an address and a count (or a value).
_FIXED_REQUEST: Final = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08})
_VARIABLE_REQUEST: Final = frozenset({0x0F, 0x10})
# A fixed request body: address and count, or address and value.
_FIXED_BODY: Final = 4
# A write-multiple request body before its data: address, count and byte count.
_VARIABLE_PREFIX: Final = 5

#: What a client may write, by function code: the manual's writable ranges
#: minus the inferred coefficients (00A4h-00ABh) and key simulation (07D0h).
#: Deliberately not derived from ``fujilib.registry.write_policy`` (design §10).
_WRITABLE: Final[Mapping[int, tuple[tuple[int, int], ...]]] = MappingProxyType(
    {
        FC_WRITE_SINGLE: ((0x0000, 0x009D), (0x07D1, 0x07D4)),
        FC_WRITE_MULTIPLE: ((0x0000, 0x00A3),),
    }
)
_COMMANDS: Final = range(0x07D1, 0x07D5)
# A reply with another function code but the same length: 03/04 and 06/10.
_OTHER_FUNCTION: Final = MappingProxyType(
    {
        FC_READ_HOLDING: FC_READ_INPUT,
        FC_READ_INPUT: FC_READ_HOLDING,
        FC_WRITE_SINGLE: FC_WRITE_MULTIPLE,
        FC_WRITE_MULTIPLE: FC_WRITE_SINGLE,
    }
)


class ExceptionProfile(StrEnum):
    """Which exception replies the simulator gives."""

    DOCUMENTED = "documented"
    """As the MODBUS manual describes: FC01 and FC02 answer exception 01."""
    BENCH_1_02 = "bench_1_02"
    """As the bench unit (firmware 1.02) answered: FC01 and FC02 answer exception 02."""


class MockWriteViolation(AssertionError):  # noqa: N818 - it fails the test, as an assertion does
    """A client sent a write fujilib must never send. Fails the test."""


@dataclass(frozen=True, slots=True)
class MockRegion:
    """Addresses ``first``..``last`` that function code ``function`` can read."""

    function: int
    first: int
    last: int

    def contains(self, address: int) -> bool:
        """Whether ``address`` lies in the region."""
        return self.first <= address <= self.last


def zp_readable_regions(
    *, observed: bool = True, firmware_2_24: bool = False
) -> tuple[MockRegion, ...]:
    """The readable map of a ZP analyzer.

    Args:
        observed: Use the bench unit's wider FC04 block 03E8h-0479h (clock,
            A/D values and the fixed settings) instead of the documented
            0425h-0469h (protocol findings §4).
        firmware_2_24: Add type-code digits 27-29 (047Ah-047Ch) and the
            calibration log (1000h-1707h).
    """
    regions = [
        MockRegion(FC_READ_HOLDING, 0x0000, 0x00AB),
        MockRegion(FC_READ_INPUT, 0x0000, 0x00C1),
        MockRegion(FC_READ_INPUT, 0x03E8, 0x0479)
        if observed
        else MockRegion(FC_READ_INPUT, 0x0425, 0x0469),
    ]
    if firmware_2_24:
        regions += [
            MockRegion(FC_READ_INPUT, 0x047A, 0x047C),
            MockRegion(FC_READ_INPUT, 0x1000, 0x1707),
        ]
    return tuple(regions)


@dataclass(frozen=True, slots=True)
class MockAnalyzerConfig:
    """What a :class:`MockAnalyzer` starts from."""

    station: int = 1
    profile: ExceptionProfile = ExceptionProfile.BENCH_1_02
    input: Mapping[int, int] = field(default_factory=lambda: MappingProxyType({}))
    """Input-register words by address; unset addresses in a region read 0."""
    holding: Mapping[int, int] = field(default_factory=lambda: MappingProxyType({}))
    """Holding-register words by address; unset addresses in a region read 0."""
    regions: tuple[MockRegion, ...] = field(default_factory=zp_readable_regions)
    description: str = ""


@dataclass(frozen=True, slots=True)
class MockRequest:
    """One request as the line received it; a frame with a bad CRC never gets here."""

    station: int
    function: int
    address: int | None
    """The start address, or ``None`` for a function code without one."""
    count: int | None
    """Words read or written, or ``None`` for a function code without a count."""
    values: tuple[int, ...]
    """The words written, for FC06 and FC10."""
    pdu: bytes
    """The request PDU: function code and body."""
    arrived_at: float
    """When the request had arrived, on the AnyIO clock."""

    @property
    def key(self) -> tuple[int, int | None, int | None]:
        """``(function, address, count)``, as a transaction list records it."""
        return (self.function, self.address, self.count)


@dataclass(slots=True)
class MockExchange:
    """A request and what the line sent back."""

    request: MockRequest
    reply: bytes | None = None
    """The reply frame, or ``None`` when nothing was sent."""
    replied_at: float | None = None
    """When the reply was sent, on the AnyIO clock."""


class FaultKind(StrEnum):
    """What goes wrong with a reply."""

    DROP = "drop"
    """No reply."""
    DELAY = "delay"
    """The reply is sent :attr:`Fault.delay_s` late (a late reply when longer than the timeout).

    The line is held up meanwhile, as a slow analyzer on a half-duplex line holds
    it up: the next request is read only after the delayed reply has gone out.
    """
    CORRUPT_CRC = "corrupt_crc"
    """The reply's CRC is wrong."""
    WRONG_COUNT = "wrong_count"
    """A well-formed read reply with one word too few (one too many for a one-word read)."""
    WRONG_FUNCTION = "wrong_function"
    """A reply that echoes another function code.

    By default a well-formed reply of the same length: 03 and 04 swap, as do 06
    and 10. :attr:`Fault.function_code` sets the code instead, e.g. 07, which
    ``anymodbus`` cannot frame at all, as a damaged byte on the line.
    """
    GARBAGE = "garbage"
    """Bytes that are not a reply: the station, then function code 0."""
    EXCEPTION = "exception"
    """An exception reply with :attr:`Fault.exception_code`."""


@dataclass(slots=True)
class Fault:
    """A fault for the replies to matching requests."""

    kind: FaultKind
    times: int | None = 1
    """How many matching requests it affects; ``None`` for every one."""
    when: Callable[[MockRequest], bool] | None = None
    """Which requests it affects; ``None`` for any."""
    delay_s: float = 0.0
    exception_code: int = 0x04
    function_code: int | None = None
    """For :attr:`FaultKind.WRONG_FUNCTION`: the code to reply with."""


class MockAnalyzer:
    """One simulated station: registers, regions, exception profile and faults."""

    def __init__(self, config: MockAnalyzerConfig | None = None) -> None:
        """Start from ``config``, or an empty analyzer at station 1."""
        self.config = config if config is not None else MockAnalyzerConfig()
        self.station = self.config.station
        self.profile = self.config.profile
        self.regions = self.config.regions
        self.input: dict[int, int] = dict(self.config.input)
        """Input-register words; change them freely."""
        self.holding: dict[int, int] = dict(self.config.holding)
        """Holding-register words; change them freely."""
        self.faults: list[Fault] = []
        """Pending faults, consulted in order."""
        self.exchanges: list[MockExchange] = []
        """Every request to this station, with its reply."""
        self.commands: list[tuple[int, int]] = []
        """Operation commands received, as ``(address, value)``."""
        self.violations: list[MockRequest] = []
        """Writes refused with :class:`MockWriteViolation`."""
        self.on_request: Callable[[MockRequest], None] | None = None
        """Called with each request before it is answered, to change state mid-sequence."""

    # --- Test controls -------------------------------------------------------------------

    def transactions(self) -> list[tuple[int, int | None, int | None]]:
        """``(function, address, count)`` of every request, in order."""
        return [e.request.key for e in self.exchanges]

    def clear(self) -> None:
        """Forget the exchanges recorded so far."""
        self.exchanges.clear()

    def set_words(self, table: RegisterTable, address: int, words: Iterable[int]) -> None:
        """Set consecutive words of ``table`` from ``address``.

        Raises:
            ValueError: a word is outside 0-0xFFFF.
        """
        bank = self.input if table is RegisterTable.INPUT else self.holding
        for offset, word in enumerate(words):
            if not 0 <= word <= _WORD_MAX:
                msg = f"register words are 0-0xFFFF, got {word}"
                raise ValueError(msg)
            bank[address + offset] = word

    def set_register(self, name: str, value: int | Sequence[int]) -> None:
        """Set the register called ``name`` in the registry; a negative value is two's complement.

        Raises:
            FujiConfigurationError: no register has that name.
            ValueError: the number of words does not match the register.
        """
        spec = REGISTRY.resolve(name)
        words = (value,) if isinstance(value, int) else tuple(value)
        if len(words) != spec.count:
            msg = f"{name} is {spec.count} word(s), got {len(words)}"
            raise ValueError(msg)
        self.set_words(spec.table, spec.address, (w & _WORD_MAX for w in words))

    def register(self, name: str) -> tuple[int, ...]:
        """The words of the register called ``name``."""
        spec = REGISTRY.resolve(name)
        bank = self.input if spec.table is RegisterTable.INPUT else self.holding
        return tuple(bank.get(a, 0) for a in range(spec.address, spec.last_address + 1))

    def set_reading(
        self,
        channel: ChannelId | str,
        raw: int,
        decimals: int,
        unit: Unit = Unit.VOL_PERCENT,
    ) -> None:
        """Set a channel's concentration, decimal point and unit."""
        n = coerce_channel(channel).number
        self.set_register(f"reading.ch{n}.value", encode_int(raw, signed=True))
        self.set_register(f"reading.ch{n}.decimals", decimals)
        self.set_register(f"reading.ch{n}.unit", unit_code(unit))

    def inject(
        self,
        kind: FaultKind,
        *,
        times: int | None = 1,
        when: Callable[[MockRequest], bool] | None = None,
        delay_s: float = 0.0,
        exception_code: int = 0x04,
        function_code: int | None = None,
    ) -> Fault:
        """Add a fault for the replies to the next matching requests."""
        fault = Fault(kind, times, when, delay_s, exception_code, function_code)
        self.faults.append(fault)
        return fault

    def take_fault(self, request: MockRequest) -> Fault | None:
        """The first pending fault that matches ``request``, used up by one."""
        for fault in self.faults:
            if fault.when is not None and not fault.when(request):
                continue
            if fault.times is not None:
                fault.times -= 1
                if fault.times <= 0:
                    self.faults.remove(fault)
            return fault
        return None

    # --- Answering -------------------------------------------------------------------------

    def handle(self, request: MockRequest, *, apply: bool = True) -> bytes:
        """The response PDU to ``request`` (an exception PDU included).

        Args:
            request: The request.
            apply: Carry out a write or command. ``False`` for a request the
                analyzer will refuse (an injected exception reply): it is still
                checked, but changes nothing.

        Raises:
            MockWriteViolation: ``request`` writes where fujilib must never write.
        """
        fc = request.function
        if fc in {FC_READ_HOLDING, FC_READ_INPUT}:
            return self._read(request)
        if fc in {FC_WRITE_SINGLE, FC_WRITE_MULTIPLE}:
            return self._write(request, apply=apply)
        if (
            fc in {_FC_READ_COILS, _FC_READ_DISCRETE}
            and self.profile is ExceptionProfile.BENCH_1_02
        ):
            return _exception(fc, _EXC_ILLEGAL_ADDRESS)
        return _exception(fc, _EXC_ILLEGAL_FUNCTION)

    def _read(self, request: MockRequest) -> bytes:
        fc, address, count = request.function, request.address or 0, request.count or 0
        if not 1 <= count <= _MAX_WORDS:
            return _exception(fc, _EXC_ILLEGAL_VALUE)
        region = next((r for r in self.regions if r.function == fc and r.contains(address)), None)
        if region is None:
            return _exception(fc, _EXC_ILLEGAL_ADDRESS)
        if address + count - 1 > region.last:
            return _exception(fc, _EXC_ILLEGAL_VALUE)
        bank = self.holding if fc == FC_READ_HOLDING else self.input
        words = [bank.get(a, 0) for a in range(address, address + count)]
        return _read_pdu(fc, words)

    def _write(self, request: MockRequest, *, apply: bool) -> bytes:
        fc, address, values = request.function, request.address or 0, request.values
        last = address + len(values) - 1
        if not any(first <= address and last <= end for first, end in _WRITABLE[fc]):
            self.violations.append(request)
            msg = (
                f"station {self.station}: FC{fc:02X} write of {len(values)} word(s) at "
                f"0x{address:04X} is outside what fujilib may ever write"
            )
            raise MockWriteViolation(msg)
        if len(values) > _MAX_WORDS:
            return _exception(fc, _EXC_ILLEGAL_VALUE)
        if apply:
            if fc == FC_WRITE_SINGLE and address in _COMMANDS:
                self.commands.append((address, values[0]))
            else:
                self.set_words(RegisterTable.HOLDING, address, values)
        if fc == FC_WRITE_SINGLE:
            return bytes((fc,)) + _word_bytes((address, values[0]))
        return bytes((fc,)) + _word_bytes((address, len(values)))

    def answer(self, exchange: MockExchange) -> tuple[bytes, Fault | None]:
        """Take in ``exchange``'s request; return the response PDU and the fault for its reply.

        Called by :class:`MockLine`. A request the fault refuses with an
        exception reply changes nothing; a reply lost or damaged on its way
        back does not undo what the analyzer already did.
        """
        self.exchanges.append(exchange)
        request = exchange.request
        if self.on_request is not None:
            self.on_request(request)
        fault = self.take_fault(request)
        refused = fault is not None and fault.kind is FaultKind.EXCEPTION
        return self.handle(request, apply=not refused), fault

    async def send_reply(
        self,
        stream: anyio.abc.ByteStream,
        exchange: MockExchange,
        pdu: bytes,
        fault: Fault | None,
    ) -> None:
        """Send the reply to ``exchange`` with ``fault`` applied. Called by :class:`MockLine`."""
        if fault is not None and fault.kind is FaultKind.DROP:
            return
        if fault is not None and fault.kind is FaultKind.DELAY:
            await anyio.sleep(fault.delay_s)
        reply = self.reply_frame(exchange.request, pdu, fault)
        await stream.send(reply)
        exchange.reply = reply
        exchange.replied_at = anyio.current_time()

    def reply_frame(self, request: MockRequest, pdu: bytes, fault: Fault | None) -> bytes:
        """The reply frame for response ``pdu``, with ``fault`` applied."""
        kind = fault.kind if fault is not None else None
        if kind is FaultKind.WRONG_COUNT and pdu[0] in {FC_READ_HOLDING, FC_READ_INPUT}:
            words = [int.from_bytes(pdu[i : i + 2], "big") for i in range(2, len(pdu), 2)]
            words = words[:-1] if len(words) > 1 else [*words, 0]
            pdu = _read_pdu(pdu[0], words)
        elif fault is not None and fault.kind is FaultKind.WRONG_FUNCTION:
            base = pdu[0] & 0x7F
            other = fault.function_code or _OTHER_FUNCTION.get(base, base)
            pdu = bytes((other | (pdu[0] & 0x80),)) + pdu[1:]
        elif fault is not None and fault.kind is FaultKind.EXCEPTION:
            pdu = _exception(request.function, fault.exception_code)
        elif kind is FaultKind.GARBAGE:
            return bytes((self.station, 0x00, 0x00, 0x00, 0x00))
        head = bytes((self.station,)) + pdu
        crc = crc16_modbus_bytes(head)
        if kind is FaultKind.CORRUPT_CRC:
            crc = bytes((crc[0] ^ 0x01, crc[1]))
        return head + crc


def _word_bytes(words: Iterable[int]) -> bytes:
    return b"".join(w.to_bytes(2, "big") for w in words)


def _read_pdu(fc: int, words: Sequence[int]) -> bytes:
    return bytes((fc, 2 * len(words))) + _word_bytes(words)


def _exception(fc: int, code: int) -> bytes:
    return bytes((fc | 0x80, code))


def _parse(station: int, pdu: bytes, arrived_at: float) -> MockRequest:
    fc = pdu[0]
    address: int | None = None
    count: int | None = None
    values: tuple[int, ...] = ()
    body = pdu[1:]
    if fc in _FIXED_REQUEST and len(body) == _FIXED_BODY:
        address = int.from_bytes(body[0:2], "big")
        second = int.from_bytes(body[2:4], "big")
        if fc == FC_WRITE_SINGLE:
            count, values = 1, (second,)
        else:
            count = second
    elif fc in _VARIABLE_REQUEST and len(body) >= _VARIABLE_PREFIX:
        address = int.from_bytes(body[0:2], "big")
        count = int.from_bytes(body[2:4], "big")
        data = body[_VARIABLE_PREFIX:]
        values = tuple(int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data) - 1, 2))
    return MockRequest(station, fc, address, count, values, pdu, arrived_at)


class _Station(MockSlave):
    """Puts a :class:`MockAnalyzer` on ``anymodbus``'s ``MockServer``."""

    # Set by handle(); the server calls send_response() only after handle().
    _answering: tuple[MockExchange, Fault | None]

    def __init__(self, analyzer: MockAnalyzer) -> None:
        # The analyzer keeps its own banks; this slave's are never used.
        super().__init__(address=analyzer.station, register_count=1, coil_count=1)
        self.analyzer = analyzer
        self.pending: list[MockExchange] = []

    def handle(self, request_pdu: bytes) -> bytes:
        if not self.pending:
            # A broadcast: the server hands it to every station without a
            # record. The ZP series documents no broadcast, so it changes nothing.
            return _exception(request_pdu[0], _EXC_ILLEGAL_FUNCTION)
        exchange = self.pending.pop()
        pdu, fault = self.analyzer.answer(exchange)
        self._answering = (exchange, fault)
        return pdu

    async def send_response(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        exchange, fault = self._answering
        await self.analyzer.send_reply(stream, exchange, response_pdu, fault)


class MockLine:
    """A simulated RS-485 line: ``anymodbus``'s ``MockServer``, requests routed by station."""

    def __init__(self, *analyzers: MockAnalyzer) -> None:
        """Put ``analyzers`` on the line.

        Raises:
            ValueError: two analyzers share a station number.
        """
        self._stations: dict[int, _Station] = {}
        self._server = MockServer(on_request=self._record)
        self.exchanges: list[MockExchange] = []
        """Every request received with a valid CRC, for any station, with its reply."""
        for analyzer in analyzers:
            self.add(analyzer)

    @property
    def stations(self) -> Mapping[int, MockAnalyzer]:
        """The analyzers on the line, by station number."""
        return MappingProxyType({n: s.analyzer for n, s in self._stations.items()})

    def add(self, analyzer: MockAnalyzer) -> None:
        """Put another analyzer on the line.

        Raises:
            ValueError: its station number is taken.
        """
        if analyzer.station in self._stations:
            msg = f"station {analyzer.station} is already on the line"
            raise ValueError(msg)
        station = _Station(analyzer)
        self._stations[analyzer.station] = station
        self._server.add(station)

    async def serve(self, stream: anyio.abc.ByteStream) -> None:
        """Answer requests on ``stream`` until it closes or the task is cancelled.

        The line ends quietly when either end closes.

        Raises:
            MockWriteViolation: a client wrote where fujilib must never write.
        """
        await self._server.serve(stream)

    def _record(self, address: int, pdu: bytes) -> None:
        exchange = MockExchange(_parse(address, pdu, anyio.current_time()))
        self.exchanges.append(exchange)
        station = self._stations.get(address)
        if station is not None:
            station.pending.append(exchange)
