"""Read-only probes of what the analyzer answers but no manual explains.

``functions`` sends, once each, the read-only diagnostic and identification
function codes the analyzer had never been sent (design §2.2): read exception
status, the diagnostics echo, the comm event counter and log, report server ID,
read file record, read FIFO queue and read device identification. Each request
is a fixed frame that reads and changes nothing; FC08 is sent with sub-function
0000h only, since its other sub-functions restart or silence the link.

``watch`` takes two full snapshots of every readable region (as
``probe_calibration.py watch`` does), then reads the status, the whole display
block (00B4h-00C1h, the "do not use" words included), the clock and A/D values
with the zero words after them, and 046Ah-0479h, about three times a second.
Every change of a display word is printed, and so is any unexplained word that
leaves 0. When the analyzer stops answering and then answers again (a power
cycle), another snapshot is taken once it has answered for ``--settle``
seconds. A file named ``SNAP`` in the output folder takes a snapshot at once;
``STOP`` ends the watch with a last snapshot. ``probe_calibration.py diff``
compares the snapshots.

Read-only by construction: ``watch`` reads through :class:`ReadOnlyStation`,
and ``functions`` can send only the frames in :data:`FUNCTION_REQUESTS`.

Usage::

    uv run python scripts/probe_unknowns.py functions --port COM8
    uv run python scripts/probe_unknowns.py watch --port COM8
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio
from _probe_common import (
    FC_READ_INPUT,
    ReadOnlyStation,
    attempt,
    open_bus,
    words_to_text,
)
from anymodbus import FrameTimeoutError, ModbusError, ModbusExceptionResponse
from probe_calibration import SCREENS, STEPS, now, out, reading, snapshot

if TYPE_CHECKING:
    from anymodbus import Bus, FunctionCode

#: The read-only requests ``functions`` may send: (name, request PDU).
FUNCTION_REQUESTS: tuple[tuple[str, bytes], ...] = (
    ("07 read exception status", bytes([0x07])),
    ("08/0000 return query data", bytes([0x08, 0x00, 0x00, 0x12, 0x34])),
    ("0B get comm event counter", bytes([0x0B])),
    ("0C get comm event log", bytes([0x0C])),
    ("11 report server ID", bytes([0x11])),
    # One record: reference type 6, file 1, record 0, length 1.
    ("14 read file record 1/0", bytes([0x14, 0x07, 0x06, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])),
    ("18 read FIFO queue at 0000h", bytes([0x18, 0x00, 0x00])),
    # Basic device identification, from object 0.
    ("2B/0E read device identification", bytes([0x2B, 0x0E, 0x01, 0x00])),
)

#: What each watch tick reads (FC04): (name, first address, word count).
WATCH_BLOCKS: tuple[tuple[str, int, int], ...] = (
    ("status", 0x0000, 61),
    ("detail", 0x0083, 0x00C1 - 0x0083 + 1),
    ("adc", 0x03E8, 0x0424 - 0x03E8 + 1),
    ("tail", 0x046A, 0x0479 - 0x046A + 1),
)

#: The display block, 00B4h-00C1h, with the names of the words already known.
DISPLAY_NAMES = {
    0xB4: "screen",
    0xB5: "step",
    0xB6: "top",
    0xB9: "result",
    0xBC: "cursor",
    0xBD: "key",
    0xBE: "alarm6",
}


def out_json(path: Path, report: dict[str, object]) -> None:
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    out(f"wrote {path}")


def run_meta(args: argparse.Namespace, probe: str) -> dict[str, object]:
    return {
        "probe": probe,
        "port": args.port,
        "address": args.address,
        "started_utc": now(),
        "python": platform.python_version(),
        "packages": {p: version(p) for p in ("anymodbus", "anyserial", "anyio")},
    }


async def send_fixed(bus: Bus, address: int, pdu: bytes) -> dict[str, object]:
    """Send one of :data:`FUNCTION_REQUESTS` and classify the reply."""
    if pdu not in {request for _, request in FUNCTION_REQUESTS}:
        msg = f"refusing {pdu.hex()}: not one of the fixed read-only requests"
        raise ValueError(msg)
    start = time.perf_counter()
    result: dict[str, object] = {"request_pdu": pdu.hex()}
    try:
        reply = await bus._txn(  # the public API has no raw request
            slave_address=address,
            request_pdu=pdu,
            expected_function_code=cast("FunctionCode", pdu[0]),
            decode=bytes,
        )
    except FrameTimeoutError:
        result["outcome"] = "silent"
    except ModbusExceptionResponse as exc:
        result |= {"outcome": "exception", "exception_code": int(exc.exception_code)}
    except ModbusError as exc:
        result |= {"outcome": "error", "detail": f"{type(exc).__name__}: {exc}"}
    else:
        body = reply[1:]
        result |= {
            "outcome": "ok",
            "reply_pdu": reply.hex(),
            "text": words_to_text(tuple(body)),
        }
    result["elapsed_ms"] = round((time.perf_counter() - start) * 1000.0, 1)
    return result


async def functions(args: argparse.Namespace) -> int:
    report = run_meta(args, "probe_unknowns functions")
    results = []
    async with open_bus(args.port, timeout=0.5, idle=0.01, retries=0) as bus:
        for name, pdu in FUNCTION_REQUESTS:
            result = await send_fixed(bus, args.address, pdu)
            results.append({"name": name, **result})
            detail = {
                "ok": f"reply {result.get('reply_pdu')}  text {result.get('text')!r}",
                "exception": f"exception {result.get('exception_code', 0):02X}h",
            }.get(str(result["outcome"]), f"{result['outcome']} {result.get('detail', '')}")
            out(f"{name:<36} {result['elapsed_ms']:6.1f} ms  {detail}")
            await anyio.sleep(0.05)
        # A normal read last, to show the link is still in step after them.
        check = await attempt(ReadOnlyStation(bus.slave(args.address)), FC_READ_INPUT, 0, 3)
        out(f"{'check: FC04 0000h x3':<36} {check.describe()}")
    report |= {"results": results, "check": check.describe()}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_json(Path(args.out) / f"probe_functions_{stamp}.json", report)
    return 0


def display_line(detail: list[int]) -> str:
    words = {a: detail[a - 0x83] for a in range(0xB4, 0xC2)}
    screen = SCREENS.get(words[0xB4], str(words[0xB4]))
    step = STEPS.get(words[0xB5], str(words[0xB5]))
    named = f"screen={screen} step={step}"
    rest = " ".join(
        f"{DISPLAY_NAMES.get(a, f'{a:04X}h')}={w}" for a, w in words.items() if a > 0xB5
    )
    return f"{named} {rest}"


def odd_words(adc: list[int], tail: list[int]) -> dict[str, int]:
    """The unexplained words that have always read 0 on the bench, when they do not."""
    found = {f"{0x03E8 + i:04X}h": w for i, w in enumerate(adc) if 0x03E8 + i >= 0x0419 and w}
    found |= {f"{0x046A + i:04X}h": w for i, w in enumerate(tail) if 0x046A + i >= 0x0472 and w}
    return found


def long_at(block: list[int], first: int, address: int) -> int:
    return block[address - first] | block[address - first + 1] << 16


def levels(status: list[int], adc: list[int], tail: list[int]) -> str:
    ir = [long_at(adc, 0x03E8, a) for a in (0x03EF, 0x03F1)]
    o2 = long_at(adc, 0x03E8, 0x03F7)
    extra = [long_at(tail, 0x046A, a) for a in (0x046A, 0x046C, 0x046E, 0x0470)]
    return (
        f"A/D No.0 {ir[0]} No.1 {ir[1]} No.4 {o2} | 046Ah.. {extra} | "
        f"Ch1 {reading(status, 1)} Ch2 {reading(status, 2)} Ch3 {reading(status, 3)}"
    )


class Watcher:
    """What one watch remembers between ticks, and what it does on each."""

    def __init__(self, args: argparse.Namespace, station: ReadOnlyStation, folder: Path) -> None:
        self.args = args
        self.station = station
        self.folder = folder
        self.snap = folder / "SNAP"
        self.second_at = time.monotonic() + args.baseline_gap
        self.last_display: str | None = None
        self.last_odd: dict[str, int] = {}
        self.failures = 0
        self.outages = 0
        self.back_since: float | None = None
        self.manual = 0
        self.beat = -args.heartbeat

    async def read(self) -> dict[str, list[int] | str]:
        """Read the watch blocks; stop at the first that fails."""
        blocks: dict[str, list[int] | str] = {}
        for name, address, count in WATCH_BLOCKS:
            result = await attempt(self.station, FC_READ_INPUT, address, count)
            blocks[name] = list(result.words) if result.ok else result.describe()
            if not result.ok:
                break
        return blocks

    def silent(self, blocks: dict[str, list[int] | str]) -> None:
        self.failures += 1
        self.back_since = None
        if self.failures == 2:
            reason = next(v for v in blocks.values() if isinstance(v, str))
            out(f"{now()}  NOT ANSWERING ({reason})")

    async def answered(self, tick: float, blocks: dict[str, list[int] | str]) -> None:
        status, detail, adc, tail = (cast("list[int]", blocks[n]) for n, _, _ in WATCH_BLOCKS)
        if self.failures >= 2:
            out(f"{now()}  answering again after {self.failures} failed reads")
            self.back_since = tick
        self.failures = 0
        line = display_line(detail)
        if line != self.last_display:
            out(f"{now()}  {line}")
            self.last_display = line
        odd = odd_words(adc, tail)
        if odd != self.last_odd:
            out(f"{now()}  UNEXPLAINED WORDS NOT ZERO: {odd or 'all zero again'}")
            self.last_odd = odd
        if tick - self.beat >= self.args.heartbeat:
            out(f"{now()}  . {levels(status, adc, tail)}")
            self.beat = tick
        await self.snapshots_due(tick)

    async def snapshots_due(self, tick: float) -> None:
        if self.second_at and tick >= self.second_at:
            await snapshot(self.station, self.folder, "before_2")
            self.second_at = 0.0
            out(f"{now()}  READY")
        if self.back_since is not None and tick - self.back_since >= self.args.settle:
            self.outages += 1
            await snapshot(self.station, self.folder, f"after_power_{self.outages}")
            self.back_since = None
        if self.snap.exists():
            self.manual += 1
            await snapshot(self.station, self.folder, f"manual_{self.manual}")
            self.snap.unlink()


async def watch(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    folder = Path(args.out) / f"unknowns_{started:%Y%m%dT%H%M%SZ}"
    folder.mkdir(parents=True, exist_ok=True)
    stop = folder / "STOP"
    meta = run_meta(args, "probe_unknowns watch")
    meta |= {"interval_s": args.interval, "settle_s": args.settle}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    out(f"writing to {folder}; create {folder / 'SNAP'} for a snapshot, {stop} to stop")
    deadline = time.monotonic() + args.max_minutes * 60
    async with open_bus(args.port, timeout=0.5, idle=0.01, retries=1) as bus:
        station = ReadOnlyStation(bus.slave(args.address))
        await snapshot(station, folder, "before_1")
        watcher = Watcher(args, station, folder)
        with (folder / "watch.jsonl").open("a", encoding="utf-8") as log:
            while time.monotonic() < deadline and not stop.exists():
                tick = time.monotonic()
                blocks = await watcher.read()
                log.write(json.dumps({"t": now(), "mono": round(tick, 3), **blocks}) + "\n")
                log.flush()
                if all(isinstance(blocks.get(n), list) for n, _, _ in WATCH_BLOCKS):
                    await watcher.answered(tick, blocks)
                else:
                    watcher.silent(blocks)
                await anyio.sleep(max(0.0, args.interval - (time.monotonic() - tick)))
        await snapshot(station, folder, "final")
    out(f"{now()}  stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    f = commands.add_parser("functions", help="send each read-only diagnostic request once")
    w = commands.add_parser("watch", help="snapshot, watch the unexplained words, snapshot")
    for p in (f, w):
        p.add_argument("--port", required=True)
        p.add_argument("--address", type=int, default=1, help="station number")
        p.add_argument("--out", default="probe_out")
    w.add_argument("--interval", type=float, default=0.3, help="seconds between watch reads")
    w.add_argument("--settle", type=float, default=15.0, help="seconds answering before a snapshot")
    w.add_argument("--baseline-gap", type=float, default=10.0, help="seconds between baselines")
    w.add_argument("--heartbeat", type=float, default=30.0, help="seconds between level lines")
    w.add_argument("--max-minutes", type=float, default=120.0)
    args = parser.parse_args(argv)
    return anyio.run(functions if args.command == "functions" else watch, args)


if __name__ == "__main__":
    sys.exit(main())
