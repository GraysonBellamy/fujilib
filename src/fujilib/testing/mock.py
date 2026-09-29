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
:class:`MockWriteViolation`, which fails the test. A write is stored as sent,
as the bench analyzer stores a value outside a setting's range, neither
refusing nor clamping it (protocol findings §13.6). A test that wants a
refusal injects an exception reply, and one that wants a write acknowledged
but not stored injects :attr:`FaultKind.IGNORE`.

**Ranges.** A channel's selected range (40106-40110), written while its range
method is manual, becomes its current range (30038-30042)
:attr:`MockAnalyzerConfig.range_lag_s` later, since the bench analyzer
switches some tens of milliseconds after the setting reads back (protocol
findings §13.2). Under the auto or remote method the current range stays.

**Operation commands** are recorded and act as the manuals describe (ZPA
manual p.31, p.46-47, p.53-60), on the AnyIO clock, with every flow time
shortened by :attr:`MockAnalyzerConfig.time_scale`:

- *return to measurement* shows the measurement screen;
- *auto calibration* zeroes the channels enabled for it (40021-40025)
  together for flow time 1, then spans them one at a time from Ch1 for flow
  times 2-6, then holds for flow time 7 if output hold is on;
- *auto zero calibration* zeroes the same channels for its flow time, and
  holds as long again if output hold is on.

While either runs, input 30049 and the channels' auto-zero or auto-span and
hold flags are set, and each enabled channel measures on its auto-calibration
range, back to its own range at the end. Errors set in
:attr:`MockAnalyzer.calibration_errors` appear at the end, as a failed
calibration's would. A command that arrives while one runs changes nothing,
and so does blowback, which has no status register. None of this is verified
on hardware: the bench analyzer has no calibration valves to drive.

**The front panel** is a test control, :meth:`MockAnalyzer.press`: an
operator pressing keys at the panel. Its manual calibration follows what the
bench analyzer showed (protocol findings §14):

- ZERO or SPAN opens channel selection (step 4 or 7) with the cursor where it
  was. UP and DOWN move it; for a zero, the channels set to "at once" share
  one position.
- ENT selects the channel: the wait step (5 or 8), the channels' zero or span
  flags, and 30186 at 0.
- ENT again runs it (6 or 9, 30186 at 4) for
  :attr:`MockAnalyzerConfig.manual_calibration_s`. It then sets each channel's
  reading to its calibration gas on its current range, and ends on the
  measurement step with 30186 at 6.
- ESC from selection or wait returns to measurement. 30190 shows each key for
  :attr:`MockAnalyzerConfig.key_hold_s`.

What the bench has not shown is written from the manuals (ZPA manual
p.64-67, p.75-77, p.89) and is unverified:

- A channel in :attr:`MockAnalyzer.calibration_errors` ends on the error
  display (step 10), with its error set and 30186 left at 4. ESC clears the
  display. ENT forces the calibration on error 5 or 7, and clears the display
  otherwise.
- Output hold sets the channels' hold flags from the wait step to the end;
  their readings are not held.
- MODE opens the menu screen and ESC closes it; no menu is modelled beyond that.

The simulator validates library integration. It does not validate USB
timing, UART behaviour on real hardware, or analyzer semantics it was
programmed to assume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
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
    "MockCalibration",
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


class _Command(IntEnum):
    RETURN_TO_MEASUREMENT = 0x07D1
    AUTO_CALIBRATION = 0x07D2
    AUTO_ZERO = 0x07D3
    BLOWBACK = 0x07D4


_ENABLED: Final = 0x14  # auto_calibration.ch1.included; Ch2-Ch5 follow
_OUTPUT_HOLD: Final = 0x5C
_AUTO_ZERO_FLOW: Final = 0x68
_AUTO_CAL_RANGE: Final = 0x73  # auto_calibration.ch1.range; Ch2-Ch5 follow
_FLOW_TIMES: Final = 0x84  # auto_calibration.flow_time1; 2-7 follow
_CURRENT_RANGE: Final = 0x25  # range.ch1.current; Ch2-Ch5 follow
_SELECTED_RANGE: Final = 0x69  # range.ch1.selected; Ch2-Ch5 follow
_RANGE_METHOD: Final = 0x6E  # range.ch1.method; Ch2-Ch5 follow
_CHANNELS: Final = range(1, 6)
_ZERO_MODE: Final = 0x19  # calibration.ch1.zero_mode; Ch2-Ch5 follow
_KEY_MODE: Final = 0x01
_KEY_UP: Final = 0x04
_KEY_DOWN: Final = 0x08
_KEY_ESC: Final = 0x10
_KEY_ENT: Final = 0x20
_KEY_ZERO: Final = 0x40
_KEY_SPAN: Final = 0x80
_SCREEN_MENU: Final = 1
_STEP_NONE: Final = 0
_STEP_ERROR: Final = 10
_SELECT_STEP: Final = MappingProxyType({_KEY_ZERO: 4, _KEY_SPAN: 7})
_SELECT_STEPS: Final = frozenset({4, 7})
_WAIT_STEPS: Final = frozenset({5, 8})
#: Errors on which ENT forces the calibration (ZPA manual p.89).
_FORCEABLE: Final = frozenset({5, 7})
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
    time_scale: float = 0.001
    """Simulated seconds per second of a calibration's flow times."""
    range_lag_s: float = 0.0
    """Seconds after a range write before the channel measures on the range selected."""
    manual_calibration_s: float = 0.0
    """Seconds a manual calibration runs after the ENT that starts it (1.6-2.4 s on the bench)."""
    key_hold_s: float = 0.3
    """Seconds 30190 shows a key pressed at the panel."""
    panel_channels: tuple[int, ...] = (1, 2, 3, 4, 5)
    """The measured channels the panel's calibration cursor offers."""


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
    IGNORE = "ignore"
    """A normal reply to a write or command that changes nothing, as a write
    the analyzer acknowledges and then does not store."""


@dataclass(frozen=True, slots=True)
class MockCalibration:
    """An auto calibration or auto zero calibration the simulator is running."""

    kind: str
    """``"auto_calibration"`` or ``"auto_zero"``."""
    started_at: float
    """On the AnyIO clock."""
    channels: tuple[int, ...]
    """The channels enabled for it, 1-5."""
    phases: tuple[tuple[str, int | None, float], ...]
    """``(phase, span channel or None, simulated seconds)``, in order."""
    hold: bool
    restore_ranges: tuple[tuple[int, int], ...]
    """``(channel, current-range word)`` to put back at the end."""

    @property
    def duration_s(self) -> float:
        """Simulated seconds from start to end."""
        return sum(p[2] for p in self.phases)

    def phase_at(self, elapsed: float) -> tuple[str, int | None] | None:
        """The phase ``elapsed`` seconds after the start, or ``None`` once finished."""
        for phase, channel, duration in self.phases:
            if elapsed < duration:
                return phase, channel
            elapsed -= duration
        return None


@dataclass(slots=True)
class _ManualCalibration:
    """A manual zero or span under way at the simulated panel."""

    zero: bool
    channels: tuple[int, ...]
    ends_at: float | None = None
    """When it finishes running, on the AnyIO clock; ``None`` until ENT runs it."""
    errors: dict[int, int] | None = None
    """The errors it ended on, while the error display shows them."""
    forced: bool = False

    @property
    def forceable(self) -> bool:
        """Whether ENT on the error display forces it (errors 5 and 7 only)."""
        return self.errors is not None and all(e in _FORCEABLE for e in self.errors.values())


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
        """Operation commands carried out, as ``(address, value)``; refused or ignored ones
        are not."""
        self.violations: list[MockRequest] = []
        """Writes refused with :class:`MockWriteViolation`."""
        self.on_request: Callable[[MockRequest], None] | None = None
        """Called with each request before it is answered, to change state mid-sequence."""
        self.calibration: MockCalibration | None = None
        """The auto calibration or auto zero calibration running, if any."""
        self.calibration_errors: dict[int, int] = {}
        """Errors 4-8 by channel, to raise when the next calibration ends."""
        self.time_scale = self.config.time_scale
        self.range_lag_s = self.config.range_lag_s
        self.range_changes: dict[int, tuple[int, float]] = {}
        """Range switches still to come, by channel: the current-range word and when."""
        self.keys: list[tuple[int, float]] = []
        """Keys pressed at the panel, as ``(key code, AnyIO time)``."""
        self._key_until: float | None = None
        self._manual: _ManualCalibration | None = None

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

    def finish_calibration(self) -> None:
        """End the running calibration now, as if its time had passed."""
        if self.calibration is not None:
            self._end_calibration(self.calibration)

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
                self._command(_Command(address), values[0], request.arrived_at)
            else:
                self.set_words(RegisterTable.HOLDING, address, values)
                self._select_ranges(address, len(values), request.arrived_at)
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
        self._switch_ranges(request.arrived_at)
        self._advance(request.arrived_at)
        self._advance_panel(request.arrived_at)
        if self.on_request is not None:
            self.on_request(request)
        fault = self.take_fault(request)
        ignored = fault is not None and fault.kind in {FaultKind.EXCEPTION, FaultKind.IGNORE}
        return self.handle(request, apply=not ignored), fault

    # --- The front panel -----------------------------------------------------------------

    def press(self, key: int, *, at: float | None = None) -> None:
        """Press ``key`` (a 42001 key code) at the front panel, now or at AnyIO time ``at``."""
        now = anyio.current_time() if at is None else at
        self._advance_panel(now)
        self.keys.append((key, now))
        self.set_register("display.key", key)
        self._key_until = now + self.config.key_hold_s
        screen = self.register("display.screen")[0]
        step = self.register("display.calibration_step")[0]
        if screen == _SCREEN_MENU:
            if key == _KEY_ESC:
                self.set_register("display.screen", 0)
        elif screen != 0:
            return
        elif step == _STEP_NONE:
            self._press_on_measurement(key)
        elif step in _SELECT_STEPS:
            self._press_on_selection(key, step)
        elif step in _WAIT_STEPS:
            self._press_on_wait(key, step, now)
        elif step == _STEP_ERROR:
            self._press_on_error(key)

    def _press_on_measurement(self, key: int) -> None:
        if key in _SELECT_STEP:
            self.set_register("display.calibration_step", _SELECT_STEP[key])
            self._put_cursor(self._positions(zero=key == _KEY_ZERO), self._cursor())
        elif key == _KEY_MODE:
            self.set_register("display.screen", _SCREEN_MENU)

    def _press_on_selection(self, key: int, step: int) -> None:
        zero = step == _SELECT_STEP[_KEY_ZERO]
        positions = self._positions(zero=zero)
        here = self._position_of(positions, self._cursor())
        if key in {_KEY_UP, _KEY_DOWN}:
            there = max(0, min(here + (1 if key == _KEY_DOWN else -1), len(positions) - 1))
            self._put_cursor(positions, positions[there][0])
        elif key == _KEY_ENT:
            channels = positions[here]
            self.set_register("display.calibration_step", step + 1)
            self.set_register("display.calibration_result", 0)
            self._set_flags(channels, zero=zero, on=True)
            self._manual = _ManualCalibration(zero=zero, channels=channels)
        elif key == _KEY_ESC:
            self.set_register("display.calibration_step", _STEP_NONE)

    def _press_on_wait(self, key: int, step: int, now: float) -> None:
        manual = self._manual
        assert manual is not None  # noqa: S101 - the wait step is reached only through ENT
        if key == _KEY_ENT:
            self.set_register("display.calibration_step", step + 1)
            self.set_register("display.calibration_result", 4)
            manual.ends_at = now + self.config.manual_calibration_s
            self._advance_panel(now)
        elif key == _KEY_ESC:
            self._set_flags(manual.channels, zero=manual.zero, on=False)
            self.set_register("display.calibration_step", _STEP_NONE)
            self._manual = None

    def _press_on_error(self, key: int) -> None:
        manual = self._manual
        if key == _KEY_ENT and manual is not None and manual.forceable:
            manual.forced = True
            self._end_manual(manual)
        elif key in {_KEY_ENT, _KEY_ESC}:
            if manual is not None:
                self._set_flags(manual.channels, zero=manual.zero, on=False)
            self.set_register("display.calibration_step", _STEP_NONE)
            self._manual = None

    def _advance_panel(self, now: float) -> None:
        if self._key_until is not None and now >= self._key_until:
            self.set_register("display.key", 0)
            self._key_until = None
        manual = self._manual
        if (
            manual is not None
            and manual.errors is None
            and manual.ends_at is not None
            and now >= manual.ends_at
        ):
            self._end_manual(manual)

    def _end_manual(self, manual: _ManualCalibration) -> None:
        errors = {c: e for c, e in self.calibration_errors.items() if c in manual.channels}
        if errors and not manual.forced:
            for c in errors:
                del self.calibration_errors[c]
            manual.errors = errors
            for c, code in errors.items():
                self.set_register(f"error.ch{c}.e{code}.active", 1)
            self.set_register("status.calibration_error", 1)
            self.set_register("display.calibration_step", _STEP_ERROR)
            return
        for c in manual.channels:
            rng = self.input.get(_CURRENT_RANGE + c - 1, 0)
            gas = self.holding.get(4 * (c - 1) + 2 * rng + (0 if manual.zero else 1), 0)
            self.set_register(f"reading.ch{c}.value", gas)
        self._set_flags(manual.channels, zero=manual.zero, on=False)
        self.set_register("display.calibration_step", _STEP_NONE)
        self.set_register("display.calibration_result", 6)
        self._manual = None

    def _set_flags(self, channels: tuple[int, ...], *, zero: bool, on: bool) -> None:
        flag = "zero_calibrating" if zero else "span_calibrating"
        hold = on and bool(self.holding.get(_OUTPUT_HOLD, 0))
        for c in channels:
            self.set_register(f"status.ch{c}.{flag}", int(on))
            self.set_register(f"status.ch{c}.hold", int(hold))

    def _positions(self, *, zero: bool) -> list[tuple[int, ...]]:
        """The cursor's positions, in order; for a zero, the "at once" channels share one."""
        channels = self.config.panel_channels
        together = tuple(c for c in channels if zero and self.holding.get(_ZERO_MODE + c - 1, 0))
        positions: list[tuple[int, ...]] = []
        for c in channels:
            if c not in together:
                positions.append((c,))
            elif c == together[0]:
                positions.append(together)
        return positions

    @staticmethod
    def _position_of(positions: Sequence[tuple[int, ...]], channel: int) -> int:
        return next((i for i, p in enumerate(positions) if channel in p), 0)

    def _cursor(self) -> int:
        return self.register("display.cursor_channel")[0] + 1

    def _put_cursor(self, positions: Sequence[tuple[int, ...]], channel: int) -> None:
        first = positions[self._position_of(positions, channel)][0]
        self.set_register("display.cursor_channel", first - 1)

    # --- Ranges -------------------------------------------------------------------------------

    def _select_ranges(self, address: int, count: int, now: float) -> None:
        """Schedule the range switch of each channel whose selected range was just written."""
        for channel in _CHANNELS:
            selected = _SELECTED_RANGE + channel - 1
            manual = self.holding.get(_RANGE_METHOD + channel - 1, 0) == 0
            if address <= selected < address + count and manual:
                self.range_changes[channel] = (self.holding[selected], now + self.range_lag_s)
        self._switch_ranges(now)

    def _switch_ranges(self, now: float) -> None:
        for channel, (word, due) in list(self.range_changes.items()):
            if due <= now:
                self.input[_CURRENT_RANGE + channel - 1] = word
                del self.range_changes[channel]

    # --- Operation commands ------------------------------------------------------------------

    def _command(self, command: _Command, value: int, now: float) -> None:
        if value != 1:
            return
        if command is _Command.RETURN_TO_MEASUREMENT:
            self.set_register("display.screen", 0)
            self.set_register("display.calibration_step", 0)
        elif command in {_Command.AUTO_CALIBRATION, _Command.AUTO_ZERO}:
            if self.calibration is None:
                self._start_calibration(command, now)

    def _flow_s(self, address: int) -> float:
        return self.holding.get(address, 0) * self.time_scale

    def _start_calibration(self, command: _Command, now: float) -> None:
        channels = tuple(c for c in range(1, 6) if self.holding.get(_ENABLED + c - 1, 0))
        hold = bool(self.holding.get(_OUTPUT_HOLD, 0))
        phases: list[tuple[str, int | None, float]]
        if command is _Command.AUTO_CALIBRATION:
            phases = [("zero", None, self._flow_s(_FLOW_TIMES))]
            phases += [("span", c, self._flow_s(_FLOW_TIMES + c)) for c in channels]
            if hold:
                phases.append(("extension", None, self._flow_s(_FLOW_TIMES + 6)))
            kind = "auto_calibration"
        else:
            phases = [("zero", None, self._flow_s(_AUTO_ZERO_FLOW))]
            if hold:
                phases.append(("extension", None, self._flow_s(_AUTO_ZERO_FLOW)))
            kind = "auto_zero"
        restore = tuple((c, self.input.get(_CURRENT_RANGE + c - 1, 0)) for c in channels)
        for c in channels:
            self.input[_CURRENT_RANGE + c - 1] = self.holding.get(_AUTO_CAL_RANGE + c - 1, 0)
        self.set_register("display.screen", 0)
        self.calibration = MockCalibration(kind, now, channels, tuple(phases), hold, restore)
        self._advance(now)

    def _advance(self, now: float) -> None:
        calibration = self.calibration
        if calibration is None:
            return
        phase = calibration.phase_at(now - calibration.started_at)
        if phase is None:
            self._end_calibration(calibration)
            return
        name, span_channel = phase
        self.set_register("status.auto_calibration_running", 1)
        for c in calibration.channels:
            self.set_register(f"status.ch{c}.auto_zero_running", int(name == "zero"))
            self.set_register(f"status.ch{c}.auto_span_running", int(span_channel == c))
            self.set_register(f"status.ch{c}.hold", int(calibration.hold))

    def _end_calibration(self, calibration: MockCalibration) -> None:
        self.calibration = None
        self.set_register("status.auto_calibration_running", 0)
        for c in calibration.channels:
            self.set_register(f"status.ch{c}.auto_zero_running", 0)
            self.set_register(f"status.ch{c}.auto_span_running", 0)
            self.set_register(f"status.ch{c}.hold", 0)
        for c, word in calibration.restore_ranges:
            self.input[_CURRENT_RANGE + c - 1] = word
        failed = False
        for c, code in self.calibration_errors.items():
            self.set_register(f"error.ch{c}.e{code}.active", 1)
            if calibration.kind == "auto_calibration":
                self.set_register(f"error.ch{c}.e9.active", 1)
            failed = True
        self.calibration_errors = {}
        if failed:
            self.set_register("status.calibration_error", 1)

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
