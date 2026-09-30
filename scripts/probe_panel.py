"""Bench probe of the front-panel keys over Modbus: what a key written to 42001 does.

**This probe writes to the analyzer.** It presses front-panel keys by writing
their codes to 42001 (07D0h), and sends return to measurement (42002). It never
starts a calibration. It sends ZERO, SPAN, UP, DOWN and ESC, and ENT only on the
channel-selection steps, where ENT selects the channel and opens the wait step
(design §6.5). It never sends:

- MODE or SIDE, which open the menus and enter their passwords;
- two keys at once;
- ENT on a wait step, where it starts the calibration, or on the error display,
  where it can force one (ZPA manual p.89);
- 42003-42005.

:class:`PanelStation` is the only door to the wire. It reads through
``_probe_common.ReadOnlyStation``. Its one write takes an address and a value
from the frozen :data:`WRITABLE`. A key is written only after a read of the panel,
in the same call, shows a step that allows it (:data:`LEGAL`), and ENT also needs
the cursor on the channel the caller names. A key is written once and never
retried: a lost reply is settled by reading the panel.

After each key the probe reads the step, the cursor, 00B9h (30186), 00BDh
(30190) and the calibration and hold flags until they settle, and records when
each changed. Anything unexpected ends the experiment. The panel is then cleaned
up for the step it is on (design §6.5):

- ESC on channel selection, a wait step or the error display;
- a wait while a calibration runs;
- 42002 on any other screen.

The cleanup then checks the flags, not only the screen. It runs in ``finally``,
shielded, with a deadline of its own, and sends each of its keys at most once.

An experiment starts only on the measurement screen with no calibration or hold
flag set. Each needs ``--confirm``, and the owner's authorization for the
session with the owner at the panel (design §13.1 #79). The experiments answer
design §13.2 #37-#39:

- ``status``: reads the panel and the settings the experiments depend on; no write.
- ``select-esc``: ZERO, ESC; SPAN, ESC.
- ``cursor``: ZERO, DOWN three times, UP three times, ESC; then the same after SPAN.
- ``zero-cancel``: ZERO, the cursor to ``--channel``, ENT (the wait step), ESC.
- ``span-cancel``: the same with SPAN.
- ``return-select``: ZERO, then 42002.
- ``return-wait``: ZERO, the cursor to ``--channel``, ENT, then 42002. The bench
  analyzer returns to measurement with the channel's zero flag still set
  (protocol findings §18.4). The probe reports it and sends nothing more; the
  operator clears it at the panel: ZERO, the channel, one ENT, ESC.
- ``at-once``: ZERO, the cursor to the channels zeroed "at once", ENT, ESC.
- ``key-lock``: with key lock on, ZERO, then ESC if it opened channel selection.
- ``backlight``: with the backlight off, the same.
- ``hold``: with output hold on, ZERO, the cursor to ``--channel``, ENT. It then
  reads the readings and the A/D values for ``--dwell`` seconds while the operator
  changes the gas, sends ESC, and reads on until the hold flags clear. Creating the
  stop file (``--stop``, ``probe_out/probe_panel.stop`` by default) ends both
  readings at once: the probe sends the ESC and stops.

``check`` runs every experiment, refusal and cleanup path against the simulator
(:class:`fujilib.testing.MockAnalyzer`) through an in-process fake station, and
``--simulate`` runs one experiment there. Neither opens a port.

Every run writes ``probe_out/probe_panel_<experiment>_<time>.json`` with the
arguments and package versions (design §11, "Evidence").

Usage::

    uv run python scripts/probe_panel.py check
    uv run python scripts/probe_panel.py --simulate zero-cancel
    uv run python scripts/probe_panel.py --port COM8 status
    uv run python scripts/probe_panel.py --port COM8 select-esc --confirm
"""

from __future__ import annotations

import argparse
import json
import platform
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import anyio
from _probe_common import FC_READ_HOLDING, FC_READ_INPUT, ReadOnlyStation, attempt, open_bus
from anymodbus import FrameTimeoutError, ModbusError, ModbusExceptionResponse

from fujilib.testing import DEFAULT_ZPA_BANK, MockAnalyzer, MockExchange, MockRequest

OUT = Path(__file__).resolve().parent.parent / "probe_out"

# --- What may be written -------------------------------------------------------------------

KEY_REGISTER: Final = 0x07D0  # 42001, keying command (TN5A1190a p.33)
RETURN_REGISTER: Final = 0x07D1  # 42002, return to measurement

UP: Final = 0x04
DOWN: Final = 0x08
ESC: Final = 0x10
ENT: Final = 0x20
ZERO: Final = 0x40
SPAN: Final = 0x80
#: The keys this probe sends. MODE (01h) and SIDE (02h) are not among them.
KEY_NAMES: Final = MappingProxyType(
    {UP: "UP", DOWN: "DOWN", ESC: "ESC", ENT: "ENT", ZERO: "ZERO", SPAN: "SPAN"}
)
#: Every write this probe can make, as (address, value).
WRITABLE: Final = frozenset({(KEY_REGISTER, k) for k in KEY_NAMES} | {(RETURN_REGISTER, 1)})

# --- The panel (TN5A1190a p.46; protocol findings §14, §15.2) ------------------------------

MEASUREMENT: Final = 0  # 30181, the measurement screen (a manual calibration included)
NONE: Final = 0  # 30182, the steps of a manual calibration on the measurement screen
ZERO_SELECT: Final = 4
ZERO_WAIT: Final = 5
ZERO_RUN: Final = 6
SPAN_SELECT: Final = 7
SPAN_WAIT: Final = 8
SPAN_RUN: Final = 9
ERROR: Final = 10
STEP_NAMES: Final = MappingProxyType(
    {
        NONE: "measurement",
        ZERO_SELECT: "zero: channel select",
        ZERO_WAIT: "zero: wait",
        ZERO_RUN: "zero: running",
        SPAN_SELECT: "span: channel select",
        SPAN_WAIT: "span: wait",
        SPAN_RUN: "span: running",
        ERROR: "error display",
    }
)
SELECT_STEPS: Final = frozenset({ZERO_SELECT, SPAN_SELECT})
WAIT_STEPS: Final = frozenset({ZERO_WAIT, SPAN_WAIT})
RUN_STEPS: Final = frozenset({ZERO_RUN, SPAN_RUN})
#: Where an experiment may stand between keys.
SAFE_STEPS: Final = frozenset({NONE}) | SELECT_STEPS | WAIT_STEPS
#: The keys each step allows, on the measurement screen only. ENT selects a channel on
#: the selection steps; on a wait step it would start the calibration.
LEGAL: Final = MappingProxyType(
    {
        NONE: frozenset({ZERO, SPAN}),
        ZERO_SELECT: frozenset({UP, DOWN, ENT, ESC}),
        SPAN_SELECT: frozenset({UP, DOWN, ENT, ESC}),
        ZERO_WAIT: frozenset({ESC}),
        SPAN_WAIT: frozenset({ESC}),
        ERROR: frozenset({ESC}),
    }
)

#: FC04 blocks read after a key: per-channel status and the display (00A5h-00BDh), then
#: the auto-calibration, zero and span flags and the error summaries (0030h-003Ch).
FAST_BLOCKS: Final = ((0x00A5, 25), (0x0030, 13))
#: FC04 blocks of a full read: readings and flags; errors, per-channel status and the
#: display; the clock and the A/D values.
FULL_BLOCKS: Final = ((0x0000, 61), (0x0083, 60), (0x03E8, 49))
CHANNELS: Final = range(1, 6)

# Holding registers the experiments depend on (TN5A1190a p.28-30).
ZERO_MODE: Final = 0x19  # 40026-40030, 1 = zeroed "at once"
RANGE_MODE: Final = 0x1E  # 40031-40035, 1 = both ranges
KEY_LOCK: Final = 0x49  # 40074
OUTPUT_HOLD: Final = 0x5C  # 40093
HOLD_MODE: Final = 0x8B  # 40140, 0 = the last value, 1 = the set value

MAX_MOVES: Final = 5


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


class Refused(Exception):  # noqa: N818 - a refusal, not an error of the probe
    """A key or write the probe will not send; nothing was written."""


class Unexpected(Exception):  # noqa: N818 - what the analyzer did, not an error of the probe
    """The analyzer did something the experiment did not expect; the experiment ends."""


# --- Reads -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Panel:
    """One read of the panel: FC04 words by address, and when they were read."""

    words: dict[int, int]
    t0: float
    t1: float
    utc: str
    failures: list[str] = field(default_factory=list)

    def _word(self, address: int) -> int | None:
        return self.words.get(address)

    def _channels(self, first: int, stride: int = 1) -> tuple[int, ...] | None:
        flags = [self.words.get(first + stride * (c - 1)) for c in CHANNELS]
        if any(f is None for f in flags):
            return None
        return tuple(c for c, f in zip(CHANNELS, flags, strict=True) if f)

    @property
    def screen(self) -> int | None:
        return self._word(0xB4)

    @property
    def step(self) -> int | None:
        return self._word(0xB5)

    @property
    def top(self) -> int | None:
        return self._word(0xB6)

    @property
    def result(self) -> int | None:
        return self._word(0xB9)

    @property
    def cursor(self) -> int | None:
        return self._word(0xBC)

    @property
    def key(self) -> int | None:
        return self._word(0xBD)

    @property
    def auto(self) -> int | None:
        return self._word(0x30)

    @property
    def zero(self) -> tuple[int, ...] | None:
        return self._channels(0x31)

    @property
    def span(self) -> tuple[int, ...] | None:
        return self._channels(0x36)

    @property
    def hold(self) -> tuple[int, ...] | None:
        return self._channels(0xA7, stride=3)

    @property
    def complete(self) -> bool:
        """Whether every word a key's check needs was read."""
        return None not in (self.screen, self.step, self.cursor, self.auto) and None not in (
            self.zero,
            self.span,
            self.hold,
        )

    @property
    def calibrating(self) -> bool:
        """An auto calibration, or a channel's zero or span flag."""
        return bool(self.auto or self.zero or self.span)

    def reading(self, channel: int) -> float | None:
        base = 3 * (channel - 1)
        raw, decimals = self.words.get(base), self.words.get(base + 1)
        if raw is None or decimals is None or decimals > 3:
            return None
        return (raw - 0x10000 if raw & 0x8000 else raw) / 10**decimals

    def adc(self, number: int) -> int | None:
        """A/D value No. ``number`` (03EFh + 2 x number), a low-word-first long word."""
        low, high = self.words.get(0x3EF + 2 * number), self.words.get(0x3F0 + 2 * number)
        return None if low is None or high is None else low | high << 16

    def signature(self) -> tuple[Any, ...]:
        """What must stop changing before a key has settled; the readings are left out."""
        return (
            self.screen,
            self.step,
            self.cursor,
            self.result,
            self.key,
            self.auto,
            self.zero,
            self.span,
            self.hold,
        )

    def fields(self) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "step": self.step,
            "top": self.top,
            "cursor": self.cursor,
            "result": self.result,
            "key": self.key,
            "auto": self.auto,
            "zero": self.zero,
            "span": self.span,
            "hold": self.hold,
            "instrument_error": self._word(0x3B),
            "calibration_error": self._word(0x3C),
        }

    def as_dict(self, *, ref: float | None = None, raw: bool = False) -> dict[str, Any]:
        doc: dict[str, Any] = {"utc": self.utc, **self.fields()}
        if ref is not None:
            doc["t0_ms"], doc["t1_ms"] = ms(self.t0 - ref), ms(self.t1 - ref)
        if 0 in self.words:
            doc["readings"] = [self.reading(c) for c in CHANNELS]
        if 0x3EF in self.words:
            doc["adc"] = [self.adc(k) for k in (0, 1, 4)]
        if raw:
            doc["words"] = {f"{a:04X}": w for a, w in sorted(self.words.items())}
        if self.failures:
            doc["failures"] = self.failures
        return doc

    def show(self) -> str:
        if not self.complete:
            return f"panel not read: {'; '.join(self.failures)}"
        step = STEP_NAMES.get(self.step, str(self.step)) if self.screen == MEASUREMENT else "-"
        text = (
            f"screen={self.screen} step={self.step} ({step}) cursor=Ch{(self.cursor or 0) + 1} "
            f"result={self.result} key={self.key} zero={_chs(self.zero)} "
            f"span={_chs(self.span)} hold={_chs(self.hold)} auto={self.auto}"
        )
        if 0 in self.words:
            text += " | " + " ".join(f"Ch{c} {self.reading(c)}" for c in (1, 2, 3))
        if 0x3EF in self.words:
            text += f" | O2 count {self.adc(4)}"
        return text


def _chs(channels: tuple[int, ...] | None) -> str:
    if channels is None:
        return "?"
    return ",".join(f"Ch{c}" for c in channels) or "-"


@dataclass(frozen=True, slots=True)
class Write:
    """One write: what was sent, when, and how the analyzer answered."""

    address: int
    value: int
    outcome: str  # "ok" | "exception" | "lost" | "error"
    sent_at: float
    answered_at: float
    utc: str
    exception_code: int | None = None
    detail: str = ""

    @property
    def name(self) -> str:
        if self.address == RETURN_REGISTER:
            return "42002"
        return KEY_NAMES.get(self.value, f"0x{self.value:02X}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "register": 40001 + self.address,
            "value": self.value,
            "name": self.name,
            "outcome": self.outcome,
            "utc": self.utc,
            "reply_ms": ms(self.answered_at - self.sent_at),
            "exception_code": self.exception_code,
            "detail": self.detail,
        }

    def describe(self) -> str:
        text = f"{self.outcome} {ms(self.answered_at - self.sent_at):.0f} ms"
        if self.exception_code is not None:
            text += f" exception {self.exception_code:02X}h"
        return f"{text} {self.detail}".rstrip()


# --- The station -----------------------------------------------------------------------------


def refusal_for(key: int, panel: Panel, cursor: int | None) -> str | None:
    """Why ``key`` may not be written on ``panel``, or ``None`` if it may."""
    name = KEY_NAMES.get(key)
    if name is None:
        return f"0x{key:02X} is not a key this probe sends"
    if not panel.complete:
        return f"the panel could not be read ({'; '.join(panel.failures)})"
    if panel.screen != MEASUREMENT:
        return f"the panel shows screen {panel.screen}, not measurement: only 42002 goes there"
    step = panel.step if panel.step is not None else -1
    if key not in LEGAL.get(step, frozenset()):
        return f"{name} is not sent on step {step} ({STEP_NAMES.get(step, 'unknown')})"
    if key in {ZERO, SPAN} and panel.calibrating:
        return f"{name} while a calibration flag is set"
    return _cursor_refusal(panel, cursor) if key == ENT else None


def _cursor_refusal(panel: Panel, cursor: int | None) -> str | None:
    """ENT selects the channel under the cursor, which must be the one the caller names."""
    if cursor is None:
        return "ENT needs the channel the cursor must be on"
    if panel.cursor != cursor - 1:
        return f"ENT with the cursor on Ch{(panel.cursor or 0) + 1}, not Ch{cursor}"
    return None


class PanelStation:
    """One station: reads, keys checked against the panel before each is written, and 42002."""

    def __init__(self, slave: Any) -> None:
        self.reads = ReadOnlyStation(slave)
        self._slave = slave
        self.writes: list[Write] = []

    async def observe(self, blocks: tuple[tuple[int, int], ...] = FAST_BLOCKS) -> Panel:
        """Read ``blocks`` (FC04) into one :class:`Panel`."""
        words: dict[int, int] = {}
        failures: list[str] = []
        utc, t0 = now(), time.monotonic()
        for address, count in blocks:
            result = await attempt(self.reads, FC_READ_INPUT, address, count)
            if result.ok:
                words.update(zip(range(address, address + count), result.words, strict=True))
            else:
                failures.append(result.describe())
        return Panel(words, t0, time.monotonic(), utc, failures)

    async def settings(self) -> dict[str, Any]:
        """The holding registers the experiments depend on."""
        words: dict[int, int] = {}
        for address, count in ((ZERO_MODE, 10), (KEY_LOCK, 1), (OUTPUT_HOLD, 1), (HOLD_MODE, 1)):
            result = await attempt(self.reads, FC_READ_HOLDING, address, count)
            if not result.ok:
                raise Refused(f"the settings could not be read: {result.describe()}")
            words.update(zip(range(address, address + count), result.words, strict=True))
        return {
            "at_once": [c for c in CHANNELS if words[ZERO_MODE + c - 1]],
            "both_ranges": [c for c in CHANNELS if words[RANGE_MODE + c - 1]],
            "key_lock": bool(words[KEY_LOCK]),
            "output_hold": bool(words[OUTPUT_HOLD]),
            "hold_mode": words[HOLD_MODE],
        }

    async def press(self, key: int, *, cursor: int | None = None) -> tuple[Panel, Write]:
        """Read the panel, then write ``key`` to 42001 once if the step allows it.

        Raises:
            Refused: the key is not one of the six, or the panel does not allow it now.
        """
        before = await self.observe()
        refusal = refusal_for(key, before, cursor)
        if refusal is not None:
            raise Refused(refusal)
        return before, await self._write(KEY_REGISTER, key)

    async def return_to_measurement(self) -> Write:
        """Write 1 to 42002 once."""
        return await self._write(RETURN_REGISTER, 1)

    async def _write(self, address: int, value: int) -> Write:
        if (address, value) not in WRITABLE:
            msg = f"FC06 {value} to {40001 + address} is not a write this probe makes"
            raise Refused(msg)
        utc, sent = now(), time.monotonic()
        outcome, code, detail = "ok", None, ""
        try:
            await self._slave.write_register(address, value)
        except FrameTimeoutError:
            outcome, detail = "lost", "no reply"
        except ModbusExceptionResponse as exc:
            outcome, code = "exception", int(exc.exception_code)
        except ModbusError as exc:
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
        write = Write(address, value, outcome, sent, time.monotonic(), utc, code, detail)
        self.writes.append(write)
        return write


# --- Following a key -------------------------------------------------------------------------


def changes(before: Panel, reads: list[Panel], ref: float) -> dict[str, Any]:
    """For each panel field, every change the reads after a write showed, and when.

    ``seen_ms`` is the end of the first read that showed the new value, and
    ``not_yet_ms`` the end of the read before it, both from the write's send.
    """
    found: dict[str, Any] = {}
    start = before.fields()
    for name in ("screen", "step", "cursor", "result", "key", "auto", "zero", "span", "hold"):
        value, last_t, seen = start[name], None, []
        for panel in reads:
            now_value = panel.fields()[name]
            if now_value is None:
                continue
            if now_value != value:
                seen.append(
                    {
                        "to": now_value,
                        "seen_ms": ms(panel.t1 - ref),
                        "not_yet_ms": None if last_t is None else ms(last_t - ref),
                    }
                )
                value = now_value
            last_t = panel.t1
        if seen:
            found[name] = {"from": start[name], "changes": seen}
    return found


def describe_changes(found: dict[str, Any]) -> str:
    parts = []
    for name, change in found.items():
        steps = ", ".join(f"{c['to']} at {c['seen_ms']:.0f}" for c in change["changes"])
        parts.append(f"{name} {change['from']} -> {steps} ms")
    return "; ".join(parts) or "nothing changed"


@dataclass(frozen=True, slots=True)
class Timing:
    """How the reads after a key are paced."""

    min_follow: float = 1.0
    """Seconds read after a key before it can count as settled."""
    quiet: float = 0.6
    """Seconds the panel must stay unchanged to count as settled."""
    max_follow: float = 5.0
    interval: float = 0.0
    """Seconds between the reads after a key; 0 reads back to back."""
    watch_interval: float = 0.5
    flag_wait: float = 5.0
    """Seconds the cleanup waits for the flags to clear on the measurement step."""
    run_wait: float = 15.0
    """Seconds the cleanup waits for a running calibration to end."""
    cleanup: float = 60.0


class Run:
    """One experiment's keys, what they did, and the cleanup."""

    def __init__(self, station: PanelStation, args: argparse.Namespace, timing: Timing) -> None:
        self.station = station
        self.args = args
        self.timing = timing
        self.events: list[dict[str, Any]] = []
        self.settings: dict[str, Any] = {}
        self.start_panel: Panel | None = None
        self.last_key: tuple[int, int | None] | None = None

    @property
    def wrote(self) -> bool:
        return bool(self.station.writes)

    async def start(self, experiment: Experiment) -> None:
        """Refuse to start unless the panel is idle and set as ``experiment`` needs."""
        panel = await self.station.observe(FULL_BLOCKS)
        self.start_panel = panel
        self.settings = await self.station.settings()
        out(f"{now()}  start: {panel.show()}")
        out(f"{now()}  settings: {self.settings}")
        problems = []
        if not panel.complete:
            problems.append("the panel could not be read")
        elif panel.screen != MEASUREMENT or panel.step != NONE:
            problems.append(f"the panel is on screen {panel.screen}, step {panel.step}")
        if panel.calibrating:
            problems.append("a calibration flag is set")
        if panel.hold:
            problems.append(f"the hold flag is set on {_chs(panel.hold)}")
        if self.settings["key_lock"] != experiment.key_lock:
            problems.append(f"key lock is {'on' if self.settings['key_lock'] else 'off'}")
        if self.settings["output_hold"] != experiment.output_hold:
            problems.append(f"output hold is {'on' if self.settings['output_hold'] else 'off'}")
        channel = self.args.channel
        if experiment.channel_each and channel in self.settings["at_once"]:
            problems.append(f"Ch{channel} is zeroed 'at once' with others")
        if experiment.at_once and len(self.settings["at_once"]) < 2:
            problems.append("fewer than two channels are zeroed 'at once'")
        if problems and experiment.body is None:
            out(f"{now()}  note: " + "; ".join(problems))
        elif problems:
            raise Refused("refusing to start: " + "; ".join(problems))

    async def follow(self, write: Write) -> tuple[list[Panel], bool]:
        """Read the panel until it settles after ``write``, or for at most ``max_follow``."""
        reads: list[Panel] = []
        last, quiet_since = None, time.monotonic()
        while True:
            panel = await self.station.observe()
            reads.append(panel)
            at = time.monotonic()
            if panel.signature() != last:
                last, quiet_since = panel.signature(), at
            since = at - write.sent_at
            if since >= self.timing.min_follow and at - quiet_since >= self.timing.quiet:
                return reads, True
            if since >= self.timing.max_follow:
                return reads, False
            await anyio.sleep(self.timing.interval)

    async def _record(self, kind: str, before: Panel, write: Write, **extra: Any) -> Panel:
        reads, settled = await self.follow(write)
        after = await self.station.observe(FULL_BLOCKS)
        found = changes(before, reads, write.sent_at)
        self.events.append(
            {
                "kind": kind,
                **extra,
                "write": write.as_dict(),
                "before": before.as_dict(),
                "settled": settled,
                "changes": found,
                "reads": [r.as_dict(ref=write.sent_at) for r in reads],
                "after": after.as_dict(raw=True),
            }
        )
        out(f"{write.utc}  {write.name:<5} {write.describe()} | {describe_changes(found)}")
        out(f"{' ' * 24}  -> {after.show()}")
        return after

    async def key(
        self,
        key: int,
        *,
        cursor: int | None = None,
        step: int | None = None,
        zero: tuple[int, ...] | None = None,
        span: tuple[int, ...] | None = None,
        refusal_ok: bool = False,
    ) -> Panel:
        """Press ``key`` once, follow it, and check the panel against what is expected."""
        before, write = await self.station.press(key, cursor=cursor)
        self.last_key = (key, before.step)
        after = await self._record("key", before, write, cursor_required=cursor)
        problems = []
        if write.outcome in {"exception", "error"} and not refusal_ok:
            problems.append(f"the write was answered: {write.describe()}")
        problems += _unexpected(after, step=step, zero=zero, span=span)
        if problems:
            raise Unexpected(f"{KEY_NAMES[key]}: " + "; ".join(problems))
        return after

    async def return_to_measurement(self) -> Panel:
        """Send 42002 once and follow it. Where it leaves the panel is what is asked."""
        before = await self.station.observe()
        write = await self.station.return_to_measurement()
        self.last_key = (RETURN_REGISTER, None)
        after = await self._record("return", before, write)
        problems = []
        if write.outcome in {"exception", "error"}:
            problems.append(f"the write was answered: {write.describe()}")
        problems += _unexpected(after)
        if problems:
            raise Unexpected("42002: " + "; ".join(problems))
        return after

    async def move_cursor(self, channel: int, step: int) -> None:
        """Move the cursor to ``channel`` with DOWN and UP, each checked to have moved it.

        The cursor wraps round at both ends, and a position shared by the channels
        zeroed "at once" reads as its first channel from below and its last from above
        (protocol findings §18). A move that comes back to a position already passed
        turns the direction round.
        """
        key: int | None = None
        passed: set[int] = set()
        for _ in range(MAX_MOVES):
            here = (await self.station.observe()).cursor
            if here is None:
                raise Unexpected("the cursor could not be read")
            if here == channel - 1:
                return
            if key is None:
                key = DOWN if here < channel - 1 else UP
            elif here in passed:
                key = UP if key == DOWN else DOWN
            passed.add(here)
            after = await self.key(key, step=step)
            if after.cursor == here:
                raise Unexpected(f"{KEY_NAMES[key]} left the cursor on Ch{here + 1}")
        raise Unexpected(f"the cursor did not reach Ch{channel} in {MAX_MOVES} moves")

    async def watch(
        self,
        label: str,
        seconds: float,
        *,
        step: int,
        until: Callable[[Panel], bool] | None = None,
        until_for: float = 3.0,
    ) -> None:
        """Read everything every ``watch_interval`` for ``seconds``, or until the stop file.

        Ends early once ``until`` has held for ``until_for`` seconds. The panel must stay
        on ``step`` throughout.
        """
        stop = Path(self.args.stop) if self.args.stop else None
        reads: list[dict[str, Any]] = []
        start = time.monotonic()
        held_since, last_shown, beat = None, None, start
        note = f"; create {stop} to end it early" if stop else ""
        out(f"{now()}  {label}: reading for up to {seconds:.0f} s{note}")
        try:
            while time.monotonic() - start < seconds and not (stop and stop.exists()):
                tick = time.monotonic()
                panel = await self.station.observe(FULL_BLOCKS)
                reads.append(panel.as_dict(ref=start))
                if panel.signature() != last_shown or tick - beat >= 10.0:
                    out(f"{panel.utc}  {label} +{tick - start:5.1f} s  {panel.show()}")
                    last_shown, beat = panel.signature(), tick
                if panel.complete and (panel.screen != MEASUREMENT or panel.step != step):
                    raise Unexpected(f"{label}: the panel left step {step}: {panel.show()}")
                if until is not None and panel.complete and until(panel):
                    held_since = tick if held_since is None else held_since
                    if tick - held_since >= until_for:
                        break
                else:
                    held_since = None
                await anyio.sleep(max(0.0, self.timing.watch_interval - (time.monotonic() - tick)))
        finally:
            self.events.append(
                {"kind": "watch", "label": label, "seconds": seconds, "reads": reads}
            )

    async def clean_up(self) -> dict[str, Any]:
        """Return the panel to measurement for the step it is on, and check the flags.

        Each cleanup key is sent at most once, and not at all when the experiment's
        last key was the same key on the same step.
        """
        actions: list[str] = []
        with anyio.CancelScope(shield=True), anyio.move_on_after(self.timing.cleanup) as scope:
            sent: set[tuple[int, int | None]] = set()
            if self.last_key is not None:
                sent.add(self.last_key)
            for _ in range(8):
                panel = await self.station.observe()
                done = await self._clean_step(panel, sent, actions)
                if done:
                    break
        end = await _shielded_observe(self.station)
        clean = end.complete and end.screen == MEASUREMENT and end.step == NONE
        clean = clean and not end.calibrating
        report = {
            "clean": clean,
            "actions": actions,
            "timed_out": scope.cancelled_caught,
            "hold": end.hold,
            "end": end.as_dict(raw=True),
        }
        out(f"{now()}  cleanup: {'; '.join(actions) or 'nothing to do'}")
        out(f"{now()}  {'CLEAN' if clean else 'NOT CLEAN: check the panel'}: {end.show()}")
        if end.hold:
            out(f"{now()}  the hold flag is still set on {_chs(end.hold)}")
        return report

    async def _clean_step(
        self, panel: Panel, sent: set[tuple[int, int | None]], actions: list[str]
    ) -> bool:
        """One cleanup action for ``panel``; ``True`` when there is nothing more to do."""
        if not panel.complete:
            actions.append("the panel could not be read")
            return False
        if panel.screen != MEASUREMENT or panel.step not in STEP_NAMES:
            return await self._clean_return(sent, actions)
        if panel.step in RUN_STEPS:
            actions.append(f"waited for step {panel.step} to end")
            return not await self._wait(lambda p: p.step not in RUN_STEPS, self.timing.run_wait)
        if panel.step != NONE:
            return await self._clean_escape(panel, sent, actions)
        if panel.calibrating:
            actions.append("waited for the calibration flags to clear")
            await self._wait(lambda p: not p.calibrating, self.timing.flag_wait)
        return True

    async def _clean_escape(
        self, panel: Panel, sent: set[tuple[int, int | None]], actions: list[str]
    ) -> bool:
        """ESC on channel selection, a wait step or the error display; 42002 if ESC was sent."""
        if (ESC, panel.step) in sent:
            return await self._clean_return(sent, actions)
        sent.add((ESC, panel.step))
        try:
            _, write = await self.station.press(ESC)
        except Refused as exc:
            actions.append(f"ESC refused: {exc}")
            return False
        actions.append(f"ESC on step {panel.step}: {write.describe()}")
        await self.follow(write)
        return False

    async def _clean_return(self, sent: set[tuple[int, int | None]], actions: list[str]) -> bool:
        if (RETURN_REGISTER, None) in sent:
            actions.append("42002 already sent; stopping")
            return True
        sent.add((RETURN_REGISTER, None))
        write = await self.station.return_to_measurement()
        actions.append(f"42002: {write.describe()}")
        await self.follow(write)
        return False

    async def _wait(self, condition: Callable[[Panel], bool], seconds: float) -> bool:
        """Read until ``condition`` holds; whether it did within ``seconds``."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            panel = await self.station.observe()
            if panel.complete and condition(panel):
                return True
            await anyio.sleep(0.2)
        return False


async def _shielded_observe(station: PanelStation) -> Panel:
    with anyio.CancelScope(shield=True):
        return await station.observe(FULL_BLOCKS)


def _unexpected(
    after: Panel,
    *,
    step: int | None = None,
    zero: tuple[int, ...] | None = None,
    span: tuple[int, ...] | None = None,
) -> list[str]:
    if not after.complete:
        return ["the panel could not be read"]
    problems = []
    if after.screen != MEASUREMENT or after.step not in SAFE_STEPS:
        problems.append(f"the panel is on screen {after.screen}, step {after.step}")
    elif step is not None and after.step != step:
        problems.append(f"step {after.step}, not {step}")
    if zero is not None and after.zero != zero:
        problems.append(f"zero flags on {_chs(after.zero)}, not {_chs(zero)}")
    if span is not None and after.span != span:
        problems.append(f"span flags on {_chs(after.span)}, not {_chs(span)}")
    return problems


# --- The experiments -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Experiment:
    """What an experiment does, and how the analyzer must be set for it."""

    body: Callable[[Run], Awaitable[None]] | None
    summary: str
    key_lock: bool = False
    output_hold: bool = False
    channel_each: bool = False
    """``--channel`` must be zeroed on its own, not "at once"."""
    at_once: bool = False
    """Two channels or more must be zeroed "at once"."""


async def select_esc(run: Run) -> None:
    await run.key(ZERO, step=ZERO_SELECT)
    await run.key(ESC, step=NONE, zero=())
    await run.key(SPAN, step=SPAN_SELECT)
    await run.key(ESC, step=NONE, span=())


async def cursor_walk(run: Run) -> None:
    for opener, step in ((ZERO, ZERO_SELECT), (SPAN, SPAN_SELECT)):
        await run.key(opener, step=step)
        for key in (DOWN, DOWN, DOWN, UP, UP, UP):
            await run.key(key, step=step)
        await run.key(ESC, step=NONE)


async def zero_cancel(run: Run) -> None:
    channel = run.args.channel
    await run.key(ZERO, step=ZERO_SELECT)
    await run.move_cursor(channel, ZERO_SELECT)
    await run.key(ENT, cursor=channel, step=ZERO_WAIT, zero=(channel,))
    await run.key(ESC, step=NONE, zero=())


async def span_cancel(run: Run) -> None:
    channel = run.args.channel
    await run.key(SPAN, step=SPAN_SELECT)
    await run.move_cursor(channel, SPAN_SELECT)
    await run.key(ENT, cursor=channel, step=SPAN_WAIT, span=(channel,))
    await run.key(ESC, step=NONE, span=())


async def return_select(run: Run) -> None:
    await run.key(ZERO, step=ZERO_SELECT)
    await run.return_to_measurement()


async def return_wait(run: Run) -> None:
    channel = run.args.channel
    await run.key(ZERO, step=ZERO_SELECT)
    await run.move_cursor(channel, ZERO_SELECT)
    await run.key(ENT, cursor=channel, step=ZERO_WAIT, zero=(channel,))
    await run.return_to_measurement()


async def at_once(run: Run) -> None:
    first = run.settings["at_once"][0]
    await run.key(ZERO, step=ZERO_SELECT)
    await run.move_cursor(first, ZERO_SELECT)
    await run.key(ENT, cursor=first, step=ZERO_WAIT)
    await run.key(ESC, step=NONE, zero=())


async def one_key(run: Run) -> None:
    after = await run.key(ZERO, refusal_ok=True)
    if after.step == ZERO_SELECT:
        await run.key(ESC, step=NONE)


async def hold(run: Run) -> None:
    channel = run.args.channel
    await run.key(ZERO, step=ZERO_SELECT)
    await run.move_cursor(channel, ZERO_SELECT)
    await run.key(ENT, cursor=channel, step=ZERO_WAIT, zero=(channel,))
    out(f"{now()}  WAIT STEP: change the gas at the inlet now")
    await run.watch("dwell", run.args.dwell, step=ZERO_WAIT)
    await run.key(ESC, step=NONE, zero=())
    await run.watch(
        "after", run.args.hold_watch, step=NONE, until=lambda p: not p.hold, until_for=5.0
    )


EXPERIMENTS: Final = MappingProxyType(
    {
        "status": Experiment(None, "read the panel and the settings; no write"),
        "select-esc": Experiment(select_esc, "ZERO, ESC; SPAN, ESC"),
        "cursor": Experiment(cursor_walk, "ZERO or SPAN, DOWN x3, UP x3, ESC"),
        "zero-cancel": Experiment(
            zero_cancel, "ZERO, cursor to --channel, ENT, ESC", channel_each=True
        ),
        "span-cancel": Experiment(span_cancel, "SPAN, cursor to --channel, ENT, ESC"),
        "return-select": Experiment(return_select, "ZERO, 42002"),
        "return-wait": Experiment(
            return_wait, "ZERO, cursor to --channel, ENT, 42002", channel_each=True
        ),
        "at-once": Experiment(
            at_once, "ZERO, cursor to the 'at once' channels, ENT, ESC", at_once=True
        ),
        "key-lock": Experiment(one_key, "with key lock on: ZERO, ESC if it acted", key_lock=True),
        "backlight": Experiment(one_key, "with the backlight off: ZERO, ESC if it acted"),
        "hold": Experiment(
            hold,
            "with output hold on: ZERO, cursor to --channel, ENT, watch, ESC, watch",
            output_hold=True,
            channel_each=True,
        ),
    }
)
#: The order of the attended session (docs/hardware-test-day.md).
SESSION: Final = (
    "select-esc",
    "cursor",
    "zero-cancel",
    "span-cancel",
    "return-select",
    "return-wait",
    "at-once",
    "key-lock",
    "backlight",
    "hold",
)


async def run_experiment(
    name: str,
    station: PanelStation,
    args: argparse.Namespace,
    timing: Timing,
    *,
    experiment: Experiment | None = None,
    simulated: bool = False,
) -> dict[str, Any]:
    """Run one experiment, clean up after it, and return its record."""
    experiment = experiment if experiment is not None else EXPERIMENTS[name]
    run = Run(station, args, timing)
    doc: dict[str, Any] = {
        "probe": "probe_panel",
        "experiment": name,
        "summary": experiment.summary,
        "simulated": simulated,
        "arguments": {k: v for k, v in vars(args).items() if k != "experiment"},
        "timing": {name: getattr(timing, name) for name in Timing.__dataclass_fields__},
        "started_at": now(),
        "versions": _versions(simulated=simulated),
    }
    out(f"{now()}  {name}: {experiment.summary}")
    stop = Path(args.stop) if args.stop else None
    if stop is not None and stop.exists():
        stop.unlink()
        out(f"{now()}  removed a stop file left from an earlier run: {stop}")
    try:
        await run.start(experiment)
        if experiment.body is not None:
            await experiment.body(run)
        doc["outcome"] = "done"
    except Refused as exc:
        doc["outcome"], doc["message"] = "refused", str(exc)
    except Unexpected as exc:
        doc["outcome"], doc["message"] = "unexpected", str(exc)
    finally:
        if run.wrote:
            doc["cleanup"] = await run.clean_up()
        doc["settings"] = run.settings
        doc["start"] = run.start_panel.as_dict(raw=True) if run.start_panel else None
        doc["events"] = run.events
        doc["writes"] = [w.as_dict() for w in station.writes]
        doc["ended_at"] = now()
    out(f"{now()}  {name}: {doc['outcome']}" + (f": {doc['message']}" if "message" in doc else ""))
    return doc


def _versions(*, simulated: bool) -> dict[str, str]:
    packages = ("anymodbus", "anyserial", "anyio") + (("fujilib",) if simulated else ())
    return {"python": platform.python_version(), **{p: version(p) for p in packages}}


# --- The simulator -----------------------------------------------------------------------------


class FakeSlave:
    """The simulator in place of an ``anymodbus`` slave, in-process.

    Reads are answered by :class:`fujilib.testing.MockAnalyzer`; a key written to
    42001 is pressed on its panel; 42002 is carried out as a command. It checks
    independently of :class:`PanelStation` what reaches the panel: a key other
    than the six, and ENT anywhere but on channel selection, are recorded in
    :attr:`hazards` and fail the write.
    """

    def __init__(self, mock: Any, *, latency: float = 0.005) -> None:
        self.mock = mock
        self.latency = latency
        self.lose_next_reply = False
        self.pressed: list[tuple[int, int, int]] = []
        """Keys pressed through the probe, as (key, screen, step) when each arrived."""
        self.hazards: list[str] = []

    def _request(self, function: int, address: int, count: int, values: tuple[int, ...]) -> Any:
        body = struct.pack(">BHH", function, address, values[0] if values else count)
        request = MockRequest(
            station=self.mock.station,
            function=function,
            address=address,
            count=count,
            values=values,
            pdu=body,
            arrived_at=anyio.current_time(),
        )
        pdu, _ = self.mock.answer(MockExchange(request))
        if pdu[0] & 0x80:
            msg = f"the simulator answered exception {pdu[1]:02X}h"
            raise RuntimeError(msg)
        return pdu

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        await anyio.sleep(self.latency)
        return struct.unpack(f">{count}H", self._request(FC_READ_INPUT, address, count, ())[2:])

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        await anyio.sleep(self.latency)
        return struct.unpack(f">{count}H", self._request(FC_READ_HOLDING, address, count, ())[2:])

    async def write_register(self, address: int, value: int) -> None:
        await anyio.sleep(self.latency)
        if address == KEY_REGISTER:
            screen = self.mock.register("display.screen")[0]
            step = self.mock.register("display.calibration_step")[0]
            if value not in KEY_NAMES or (
                value == ENT and (screen != MEASUREMENT or step not in SELECT_STEPS)
            ):
                self.hazards.append(f"0x{value:02X} on screen {screen}, step {step}")
                msg = f"the probe sent 0x{value:02X} on screen {screen}, step {step}"
                raise AssertionError(msg)
            self.pressed.append((value, screen, step))
            self.mock.press(value)
        else:
            self._request(0x06, address, 1, (value,))
        if self.lose_next_reply:
            self.lose_next_reply = False
            msg = "reply lost (simulated)"
            raise FrameTimeoutError(msg)


def bench_simulator(**config: Any) -> Any:
    """The bench analyzer on the simulator, set as the bench was on 2026-09-29."""
    options = {"panel_channels": (1, 2, 3), "key_hold_s": 0.15, "manual_calibration_s": 0.3}
    mock = MockAnalyzer(replace(DEFAULT_ZPA_BANK, **(options | config)))
    for c, together in zip(CHANNELS, (1, 1, 0, 1, 1), strict=True):
        mock.holding[ZERO_MODE + c - 1] = together
        mock.holding[RANGE_MODE + c - 1] = 0
    mock.holding[KEY_LOCK] = 0
    mock.holding[OUTPUT_HOLD] = 0
    mock.set_register("display.key", 0)
    return mock


SIMULATED_TIMING: Final = Timing(
    min_follow=0.25, quiet=0.15, max_follow=2.0, watch_interval=0.1, flag_wait=0.5, run_wait=3.0
)


def simulated_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(**(vars(args) | {"dwell": 0.6, "hold_watch": 2.0, "stop": ""}))


async def check(args: argparse.Namespace) -> int:
    """Every experiment, refusal and cleanup path on the simulator; 1 if any fails."""
    failures: list[str] = []

    def verdict(name: str, ok: bool, detail: str = "") -> None:
        out(f"{'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    sim = simulated_args(args)
    await _check_session(sim, verdict)
    await _check_return_wait(sim, verdict)
    await _check_refusals(verdict)
    await _check_cleanups(sim, verdict)
    out()
    out(f"{len(failures)} check(s) failed: {failures}" if failures else "every check passed")
    return 1 if failures else 0


Verdict = Callable[..., None]


async def _check_session(sim: argparse.Namespace, verdict: Verdict) -> None:
    """The session's experiments, in order, on one simulated analyzer."""
    mock = bench_simulator()
    slave = FakeSlave(mock)
    for name in SESSION:
        if name == "return-wait":
            continue
        mock.holding[KEY_LOCK] = int(name == "key-lock")
        mock.holding[OUTPUT_HOLD] = int(name == "hold")
        station = PanelStation(slave)
        doc = await run_experiment(name, station, sim, SIMULATED_TIMING, simulated=True)
        ok = doc["outcome"] == "done" and doc["cleanup"]["clean"]
        verdict(f"experiment {name}", ok, doc.get("message", ""))
        if name == "at-once":
            ent = next(e for e in doc["events"] if e.get("write", {}).get("name") == "ENT")
            verdict(
                "at-once: ENT set the zero flags of Ch1 and Ch2", ent["after"]["zero"] == (1, 2)
            )
        if name == "hold":
            ent = next(e for e in doc["events"] if e.get("write", {}).get("name") == "ENT")
            verdict("hold: the wait step set Ch3's hold flag", ent["after"]["hold"] == (3,))
    mock.holding[KEY_LOCK] = mock.holding[OUTPUT_HOLD] = 0
    verdict("only the six keys reached the panel", all(k in KEY_NAMES for k, _, _ in slave.pressed))
    verdict("no ENT reached a wait step or the error display", not slave.hazards)
    verdict(
        "every ENT arrived on channel selection",
        all(s in SELECT_STEPS for k, _, s in slave.pressed if k == ENT),
    )


async def _check_return_wait(sim: argparse.Namespace, verdict: Verdict) -> None:
    """42002 on the wait step. The simulator leaves the flags set (it does not model it)."""
    mock = bench_simulator()
    slave = FakeSlave(mock)
    doc = await run_experiment(
        "return-wait", PanelStation(slave), sim, SIMULATED_TIMING, simulated=True
    )
    keys = [k for k, _, _ in slave.pressed]
    verdict("return-wait: ran", doc["outcome"] == "done", doc.get("message", ""))
    verdict(
        "return-wait: flags left set are reported, not cleaned by more keys",
        not doc["cleanup"]["clean"] and keys[-1] == ENT and ESC not in keys,
        str(doc["cleanup"]["actions"]),
    )


async def _check_refusals(verdict: Verdict) -> None:
    """Keys and writes the station must refuse, with nothing written."""
    mock = bench_simulator()
    mock.set_register("display.cursor_channel", 0)
    slave = FakeSlave(mock)
    station = PanelStation(slave)

    async def refused(name: str, call: Callable[[], Awaitable[Any]]) -> None:
        count = len(station.writes)
        try:
            await call()
        except Refused as exc:
            verdict(f"refused: {name}", len(station.writes) == count, str(exc))
        else:
            verdict(f"refused: {name}", False, "it was written")

    for key in (0x01, 0x02, 0x00, ZERO | ENT, UP | DOWN, 0x100):
        await refused(f"key 0x{key:02X}", lambda k=key: station.press(k))
    await refused("42003", lambda: station._write(0x07D2, 1))
    await refused("42002 = 0", lambda: station._write(RETURN_REGISTER, 0))
    await refused("42001 = MODE", lambda: station._write(KEY_REGISTER, 0x01))
    for key in (ENT, ESC, UP, DOWN):
        await refused(f"{KEY_NAMES[key]} on measurement", lambda k=key: station.press(k, cursor=1))
    await station.press(ZERO)
    await refused("ZERO on channel selection", lambda: station.press(ZERO))
    await refused("ENT without a channel", lambda: station.press(ENT))
    await refused("ENT on the wrong channel", lambda: station.press(ENT, cursor=3))
    await station.press(DOWN)
    await station.press(ENT, cursor=3)
    for key in (ENT, UP, DOWN, ZERO, SPAN):
        await refused(
            f"{KEY_NAMES[key]} on the wait step", lambda k=key: station.press(k, cursor=3)
        )
    await station.press(ESC)
    mock.set_register("display.calibration_step", ERROR)
    for key in (ENT, UP, ZERO):
        await refused(
            f"{KEY_NAMES[key]} on the error display", lambda k=key: station.press(k, cursor=3)
        )
    mock.set_register("display.calibration_step", NONE)
    mock.set_register("display.screen", 1)
    for key in (ESC, ZERO):
        await refused(f"{KEY_NAMES[key]} on a menu", lambda k=key: station.press(k))
    await station.return_to_measurement()
    mock.set_register("status.ch2.zero_calibrating", 1)
    await refused("ZERO with a flag set", lambda: station.press(ZERO))
    mock.set_register("status.ch2.zero_calibrating", 0)
    verdict("refusals: nothing hazardous reached the panel", not slave.hazards)


async def _check_cleanups(sim: argparse.Namespace, verdict: Verdict) -> None:
    """Each cleanup path, from an experiment that stops where the path starts."""

    async def case(
        name: str,
        body: Callable[[Run], Awaitable[None]],
        *,
        setup: Callable[[Any], None] | None = None,
        clean: bool = True,
        outcome: str = "unexpected",
    ) -> dict[str, Any]:
        mock = bench_simulator()
        if setup is not None:
            setup(mock)
        slave = FakeSlave(mock)
        experiment = Experiment(body, name)
        doc = await run_experiment(
            name, PanelStation(slave), sim, SIMULATED_TIMING, experiment=experiment, simulated=True
        )
        got = doc.get("cleanup", {}).get("clean", True)
        ok = doc["outcome"] == outcome and got == clean and not slave.hazards
        verdict(f"cleanup: {name}", ok, str(doc.get("cleanup", {}).get("actions", "")))
        return doc

    async def stop_on_wait(run: Run) -> None:
        await run.key(ZERO, step=ZERO_SELECT)
        await run.move_cursor(3, ZERO_SELECT)
        await run.key(ENT, cursor=3, step=ZERO_WAIT)
        raise Unexpected("stopped on the wait step")

    async def stop_on_select(run: Run) -> None:
        await run.key(SPAN, step=SPAN_SELECT)
        raise Unexpected("stopped on channel selection")

    async def stop_on_menu(run: Run) -> None:
        await run.key(ZERO, step=ZERO_SELECT)
        run.station._slave.mock.set_register("display.screen", 1)
        raise Unexpected("a menu opened")

    async def owner_runs(run: Run) -> None:
        await run.key(ZERO, step=ZERO_SELECT)
        await run.move_cursor(3, ZERO_SELECT)
        await run.key(ENT, cursor=3, step=ZERO_WAIT)
        run.station._slave.mock.press(ENT)  # the operator, at the panel
        raise Unexpected("a calibration started at the panel")

    async def lost_reply(run: Run) -> None:
        run.station._slave.lose_next_reply = True
        await run.key(ZERO, step=ZERO_SELECT)
        await run.key(ESC, step=NONE)

    def fails(mock: Any) -> None:
        mock.calibration_errors[3] = 5

    await case("from the wait step", stop_on_wait)
    await case("from channel selection", stop_on_select)
    await case("from a menu", stop_on_menu)
    await case("while a calibration runs", owner_runs)
    await case("from the error display", owner_runs, setup=fails)
    doc = await case("a lost reply", lost_reply, outcome="done")
    lost = [w for w in doc["writes"] if w["outcome"] == "lost"]
    verdict(
        "a lost reply: settled by reading, not resent", len(lost) == 1 and len(doc["writes"]) == 2
    )

    def on_menu(mock: Any) -> None:
        mock.set_register("display.screen", 1)

    def flagged(mock: Any) -> None:
        mock.set_register("status.ch3.zero_calibrating", 1)

    for label, setup in (("a menu", on_menu), ("a flag set", flagged)):
        doc = await case(f"refused to start on {label}", select_esc, setup=setup, outcome="refused")
        verdict(f"refused to start on {label}: nothing written", not doc["writes"])


# --- Main --------------------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    timing = Timing()
    if args.simulate:
        station = PanelStation(FakeSlave(bench_simulator()))
        return await run_experiment(
            args.experiment, station, simulated_args(args), SIMULATED_TIMING, simulated=True
        )
    async with open_bus(args.port, timeout=0.5, idle=0.01, retries=2) as bus:
        if not bus.config.retries.retry_idempotent_only:
            msg = "the bus would retry writes"
            raise SystemExit(msg)
        station = PanelStation(bus.slave(args.address))
        return await run_experiment(args.experiment, station, args, timing)


def save(doc: dict[str, Any], args: argparse.Namespace) -> Path:
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    sim = "sim_" if doc["simulated"] else ""
    path = folder / f"probe_panel_{sim}{doc['experiment']}_{stamp}.json"
    path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("experiment", choices=[*EXPERIMENTS, "check"])
    parser.add_argument("--port", help="the analyzer's port, e.g. COM8")
    parser.add_argument("--address", type=int, default=1, help="station number")
    parser.add_argument("--confirm", action="store_true", help="Required: this presses keys.")
    parser.add_argument("--simulate", action="store_true", help="run on the simulator")
    parser.add_argument("--channel", type=int, default=3, help="the channel to select")
    parser.add_argument("--dwell", type=float, default=120.0, help="seconds on the wait step")
    parser.add_argument(
        "--hold-watch", type=float, default=420.0, help="seconds to wait for the hold to end"
    )
    parser.add_argument(
        "--stop",
        default=str(OUT / "probe_panel.stop"),
        help="end a watch early when this file exists",
    )
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    if args.experiment == "check":
        return anyio.run(check, args)
    if not args.simulate:
        if not args.port:
            parser.error("--port is required, or --simulate")
        if EXPERIMENTS[args.experiment].body is not None and not args.confirm:
            parser.error("this experiment presses keys on the analyzer; pass --confirm")
    doc = anyio.run(main_async, args)
    out(f"wrote {save(doc, args)}")
    clean = doc.get("cleanup", {}).get("clean", True)
    return 0 if doc["outcome"] == "done" and clean else 1


if __name__ == "__main__":
    sys.exit(main())
