"""Read-only watch of a manual calibration made at the front panel.

Finds out whether the analyzer keeps anything of a calibration in the registers
it answers but the manual does not document (design §2.3), and records what the
panel and the status registers do while an operator calibrates.

``watch`` takes two full snapshots of every readable region (the documented
ones, the undocumented clock and A/D block, and the two undocumented FC03
factory blocks), then reads the display state, the calibration flags, the
readings and the A/D values about twice a second. Every change of the panel
state is printed. Each time the analyzer is quiet again after being busy (a
calibration step shown, a calibration or hold flag set, or a menu open) for
``--settle`` seconds, another snapshot is taken. It stops when the file named
by ``--stop`` exists, or after ``--max-minutes``, with a last snapshot.

``diff`` compares the snapshots of a watch: the words that changed between the
two first snapshots are live, and every other word that changed afterwards is
listed, with the pair read as a low-word-first long word beside it.

Read-only by construction: see :mod:`_probe_common`.

Usage::

    uv run python scripts/probe_calibration.py watch --port COM8
    uv run python scripts/probe_calibration.py diff probe_out/calwatch_<time>
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import anyio
from _probe_common import (
    FC_READ_HOLDING,
    FC_READ_INPUT,
    MAX_WORDS,
    ReadOnlyStation,
    attempt,
    open_bus,
    to_int16,
)

#: (function code, first address, word count): every region the bench unit answers
#: (protocol findings §4), the undocumented ones included.
SNAPSHOT_REGIONS: tuple[tuple[int, int, int], ...] = (
    (FC_READ_INPUT, 0x0000, 0x00C2),
    (FC_READ_INPUT, 0x03E8, 0x0479 - 0x03E8 + 1),
    (FC_READ_HOLDING, 0x0000, 0x00AC),
    (FC_READ_HOLDING, 0x03E8, 0x069B - 0x03E8 + 1),
    (FC_READ_HOLDING, 0x0BB8, 0x0C66 - 0x0BB8 + 1),
)

#: What each watch tick reads (FC04): readings, ranges and calibration flags;
#: errors, per-channel status and the display; the clock and the A/D values.
WATCH_BLOCKS: tuple[tuple[str, int, int], ...] = (
    ("status", 0x0000, 61),
    ("detail", 0x0083, 60),
    ("adc", 0x03E8, 0x0418 - 0x03E8 + 1),
)

SCREENS = {
    0: "measurement",
    1: "menu",
    2: "range change",
    3: "calibration setting",
    4: "alarm setting",
    5: "auto calibration setting",
    6: "peak alarm setting",
    7: "parameter setting",
    8: "MAINTENANCE",
    9: "FACTORY",
    10: "auto zero setting",
}
STEPS = {
    0: "none",
    4: "zero: channel select",
    5: "zero: wait",
    6: "zero: running",
    7: "span: channel select",
    8: "span: wait",
    9: "span: running",
    10: "error display",
}


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def key(fc: int, address: int) -> str:
    return f"FC{fc:02X}:{address:04X}"


async def snapshot(station: ReadOnlyStation, folder: Path, label: str) -> None:
    """Read every region in blocks of at most 64 words and write it to ``folder``."""
    started = now()
    start = time.perf_counter()
    words: dict[str, int] = {}
    failures: list[str] = []
    for fc, first, count in SNAPSHOT_REGIONS:
        address = first
        while address < first + count:
            size = min(MAX_WORDS, first + count - address)
            result = await attempt(station, fc, address, size)
            if result.ok:
                for offset, word in enumerate(result.words):
                    words[key(fc, address + offset)] = word
            else:
                failures.append(result.describe())
            address += size
    elapsed = time.perf_counter() - start
    path = folder / f"snapshot_{label}.json"
    report = {"label": label, "started_utc": started, "elapsed_s": round(elapsed, 3)}
    report |= {"failures": failures, "words": words}
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    note = f", {len(failures)} block(s) FAILED" if failures else ""
    out(f"{now()}  snapshot {label}: {len(words)} words in {elapsed:.2f} s{note}")


@dataclass(frozen=True, slots=True)
class PanelState:
    """The panel and calibration state that a change is printed for."""

    screen: int
    step: int
    top: int
    cursor: int
    auto_running: int
    zero: tuple[int, ...]
    span: tuple[int, ...]
    auto_zero: tuple[int, ...]
    auto_span: tuple[int, ...]
    hold: tuple[int, ...]
    instrument_error: int
    calibration_error: int
    errors: tuple[int, ...]

    @classmethod
    def read(cls, status: tuple[int, ...], detail: tuple[int, ...]) -> PanelState:
        def at(address: int) -> int:
            return detail[address - 0x83]

        return cls(
            screen=at(0xB4),
            step=at(0xB5),
            top=at(0xB6),
            cursor=at(0xBC),
            auto_running=status[0x30],
            zero=tuple(status[0x31:0x36]),
            span=tuple(status[0x36:0x3B]),
            auto_zero=tuple(at(0xA5 + 3 * c) for c in range(5)),
            auto_span=tuple(at(0xA6 + 3 * c) for c in range(5)),
            hold=tuple(at(0xA7 + 3 * c) for c in range(5)),
            instrument_error=status[0x3B],
            calibration_error=status[0x3C],
            errors=tuple(at(a) for a in range(0x87, 0xA5)),
        )

    @property
    def busy(self) -> bool:
        """A menu or calibration step is shown, or a calibration or hold flag is set."""
        flags = (*self.zero, *self.span, *self.auto_zero, *self.auto_span, *self.hold)
        return bool(self.screen or self.step or self.auto_running or any(flags))

    def show(self, status: tuple[int, ...]) -> str:
        active = [f"ch{1 + i // 6}:e{4 + i % 6}" for i, v in enumerate(self.errors) if v]
        screen = SCREENS.get(self.screen, str(self.screen))
        step = STEPS.get(self.step, str(self.step))
        return (
            f"screen={screen:<12} step={step:<22} cursor=Ch{self.cursor + 1} "
            f"top=Ch{self.top + 1} auto={self.auto_running} zero={self.zero} "
            f"span={self.span} autoz={self.auto_zero} autos={self.auto_span} "
            f"hold={self.hold} inst_err={self.instrument_error} "
            f"cal_err={self.calibration_error} errors={','.join(active) or '-'} | "
            f"Ch1 {reading(status, 1)} Ch2 {reading(status, 2)} Ch3 {reading(status, 3)}"
        )


def reading(status: tuple[int, ...], channel: int) -> str:
    base = 3 * (channel - 1)
    raw, decimals = to_int16(status[base]), status[base + 1]
    return f"{raw / 10**decimals:.{decimals}f}" if decimals <= 3 else f"raw {raw}"


class Watch:
    """The watch loop's state: what the panel showed, and which snapshots are due."""

    def __init__(self, station: ReadOnlyStation, folder: Path, args: argparse.Namespace) -> None:
        self.station = station
        self.folder = folder
        self.args = args
        self.last: PanelState | None = None
        self.saw_busy = False
        self.quiet_since: float | None = None
        self.taken = 0
        self.second_at = time.monotonic() + args.baseline_gap
        self.second_done = False
        self.beat = -args.heartbeat

    async def read(self) -> dict[str, list[int] | str]:
        """One watch read: every block of :data:`WATCH_BLOCKS`, or why it failed."""
        blocks: dict[str, list[int] | str] = {}
        for name, address, count in WATCH_BLOCKS:
            result = await attempt(self.station, FC_READ_INPUT, address, count)
            blocks[name] = list(result.words) if result.ok else result.describe()
        return blocks

    async def take_in(self, tick: float, status: list[int], detail: list[int]) -> None:
        """Print a change of state and take the snapshots that are due."""
        state = PanelState.read(tuple(status), tuple(detail))
        changed = state != self.last
        if changed or tick - self.beat >= self.args.heartbeat:
            out(f"{now()}  {'' if changed else '. '}{state.show(tuple(status))}")
            self.beat = tick
        self.last = state
        if state.busy:
            self.saw_busy, self.quiet_since = True, None
        elif self.quiet_since is None:
            self.quiet_since = tick
        if not self.second_done and self.saw_busy:
            self.second_done = True
            out(f"{now()}  busy before the second baseline; there will be none")
        if not self.second_done and tick >= self.second_at:
            await snapshot(self.station, self.folder, "before_2")
            self.second_done = True
            out(f"{now()}  READY: calibrate at the panel whenever you like")
        elif (
            self.saw_busy
            and self.quiet_since is not None
            and tick - self.quiet_since >= self.args.settle
        ):
            self.taken += 1
            await snapshot(self.station, self.folder, f"after_{self.taken}")
            self.saw_busy = False


async def watch(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    folder = Path(args.out) / f"calwatch_{started:%Y%m%dT%H%M%SZ}"
    folder.mkdir(parents=True, exist_ok=True)
    stop = Path(args.stop) if args.stop else folder / "STOP"
    meta = {
        "probe": "probe_calibration watch",
        "port": args.port,
        "address": args.address,
        "started_utc": started.isoformat(),
        "interval_s": args.interval,
        "settle_s": args.settle,
        "python": platform.python_version(),
        "packages": {p: version(p) for p in ("anymodbus", "anyserial", "anyio")},
        "stop_file": str(stop),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    out(f"writing to {folder}; create {stop} to stop")
    deadline = time.monotonic() + args.max_minutes * 60
    async with open_bus(args.port, timeout=0.5, idle=0.01, retries=2) as bus:
        station = ReadOnlyStation(bus.slave(args.address))
        await snapshot(station, folder, "before_1")
        loop = Watch(station, folder, args)
        with (folder / "watch.jsonl").open("a", encoding="utf-8") as log:
            while time.monotonic() < deadline and not stop.exists():
                tick = time.monotonic()
                blocks = await loop.read()
                log.write(json.dumps({"t": now(), "mono": round(tick, 3), **blocks}) + "\n")
                status, detail = blocks["status"], blocks["detail"]
                if isinstance(status, list) and isinstance(detail, list):
                    await loop.take_in(tick, status, detail)
                else:
                    out(f"{now()}  read failed: {status if isinstance(status, str) else detail}")
                await anyio.sleep(max(0.0, args.interval - (time.monotonic() - tick)))
        await snapshot(station, folder, "final")
    out(f"{now()}  stopped")
    return 0


def neighbour_long(words: dict[str, int], name: str) -> str:
    """The word read with its neighbours as low-word-first long words."""
    fc, address = name.split(":")
    here = int(address, 16)
    parts = []
    for low in (here - 1, here):
        lo, hi = words.get(f"{fc}:{low:04X}"), words.get(f"{fc}:{low + 1:04X}")
        if lo is not None and hi is not None:
            parts.append(f"[{low:04X}]={lo | hi << 16}")
    return " ".join(parts)


def diff(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    snapshots = {
        p.stem.removeprefix("snapshot_"): json.loads(p.read_text(encoding="utf-8"))
        for p in folder.glob("snapshot_*.json")
    }
    order = sorted(snapshots, key=lambda label: snapshots[label]["started_utc"])
    first = dict(snapshots["before_1"]["words"])
    if "before_2" in snapshots:
        base = dict(snapshots["before_2"]["words"])
        live = {k for k in first if first.get(k) != base.get(k)}
        out(f"live words (changed between before_1 and before_2): {len(live)}")
        for name in sorted(live):
            out(f"  {name}  {first[name]:6d} -> {base[name]:6d}")
    else:
        base, live = first, set()
        out("no before_2: every changed word is listed, live ones included")
    for label in order:
        if label.startswith("before"):
            continue
        after = dict(snapshots[label]["words"])
        changed = sorted(k for k in after if after.get(k) != base.get(k) and k not in live)
        out()
        out(f"{label}: {len(changed)} word(s) changed since the baseline, live words left out")
        for name in changed:
            old, new = base.get(name), after[name]
            delta = new - old if old is not None else None
            out(
                f"  {name}  {old!s:>6} -> {new:6d}  (delta {delta!s:>6}, "
                f"int16 {to_int16(new)})  long: {neighbour_long(base, name)} -> "
                f"{neighbour_long(after, name)}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    w = commands.add_parser("watch", help="snapshot, watch the panel, snapshot again")
    w.add_argument("--port", required=True)
    w.add_argument("--address", type=int, default=1, help="station number")
    w.add_argument("--interval", type=float, default=0.5, help="seconds between watch reads")
    w.add_argument("--settle", type=float, default=20.0, help="quiet seconds before a snapshot")
    w.add_argument("--baseline-gap", type=float, default=10.0, help="seconds between baselines")
    w.add_argument("--heartbeat", type=float, default=30.0, help="seconds between status lines")
    w.add_argument("--max-minutes", type=float, default=60.0)
    w.add_argument("--stop", default="", help="stop when this file exists")
    w.add_argument("--out", default="probe_out")
    d = commands.add_parser("diff", help="compare the snapshots of a watch")
    d.add_argument("folder")
    args = parser.parse_args(argv)
    if args.command == "watch":
        return anyio.run(watch, args)
    return diff(args)


if __name__ == "__main__":
    sys.exit(main())
