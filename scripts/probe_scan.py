"""Read-only address-space scan for a Fuji ZP-series gas analyzer.

Reads one word at a time across an address range with FC03 or FC04 and records,
for every address, either the value or the exception code. The result is the
analyzer's *actual* readable map, which on the bench unit differs from the manual.

Read-only by construction: see :mod:`_probe_common`.

Usage::

    uv run --no-project --with anymodbus --with anyserial \
        python scripts/probe_scan.py --port COM8 --fc 4 --start 0x0000 --end 0x2000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import anyio
from _probe_common import ReadOnlyStation, attempt, open_bus, to_int16


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def runs(addresses: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted address list into inclusive (first, last) runs."""
    result: list[tuple[int, int]] = []
    for address in addresses:
        if result and address == result[-1][1] + 1:
            result[-1] = (result[-1][0], address)
        else:
            result.append((address, address))
    return result


def render(word: int) -> str:
    low = word & 0xFF
    char = chr(low) if 0x20 <= low < 0x7F and word < 0x100 else " "
    return f"{word:04X} {to_int16(word):6d} {char}"


async def run(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    values: dict[int, int] = {}
    exceptions: dict[int, int] = {}
    other: dict[int, str] = {}
    t0 = time.perf_counter()
    async with open_bus(
        args.port, timeout=args.timeout, idle=args.idle, retries=args.retries
    ) as bus:
        station = ReadOnlyStation(bus.slave(args.address))
        for address in range(args.start, args.end, args.step):
            result = await attempt(station, args.fc, address, 1)
            if result.ok:
                values[address] = result.words[0]
            elif result.outcome == "exception" and result.exception_code is not None:
                exceptions[address] = result.exception_code
            else:
                other[address] = f"{result.outcome} {result.detail}".strip()
    elapsed = time.perf_counter() - t0
    total = len(values) + len(exceptions) + len(other)
    out(
        f"fc{args.fc:02X} {args.start:04X}h..{args.end - 1:04X}h step {args.step}: "
        f"{total} probes in {elapsed:.1f} s ({1000 * elapsed / max(total, 1):.1f} ms each)"
    )
    out(f"readable: {len(values)}   exception: {len(exceptions)}   silent/error: {len(other)}")

    out("\nreadable runs:")
    for first, last in runs(sorted(values)):
        out(f"  {first:04X}h..{last:04X}h  ({last - first + 1} word(s))")
    codes = sorted(set(exceptions.values()))
    out(f"exception codes seen: {[f'{c:02X}h' for c in codes]}")
    if other:
        out(f"silent/error addresses: {[f'{a:04X}' for a in sorted(other)][:40]}")

    if args.dump:
        out("\naddr   hex   int16 chr")
        for address in sorted(values):
            if args.nonzero and values[address] == 0:
                continue
            out(f"{address:04X}h  {render(values[address])}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = (
        out_dir / f"probe_scan_fc{args.fc:02d}_{args.start:04X}_{args.end:04X}_"
        f"{started:%Y%m%dT%H%M%SZ}.json"
    )
    path.write_text(
        json.dumps(
            {
                "probe": "probe_scan",
                "port": args.port,
                "address": args.address,
                "fc": args.fc,
                "start": args.start,
                "end": args.end,
                "step": args.step,
                "started_utc": started.isoformat(),
                "values": {f"{a:04X}": v for a, v in sorted(values.items())},
                "exceptions": {f"{a:04X}": c for a, c in sorted(exceptions.items())},
                "other": {f"{a:04X}": t for a, t in sorted(other.items())},
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    out(f"\nraw results written to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1, help="station number")
    parser.add_argument("--fc", type=int, choices=(3, 4), required=True)
    parser.add_argument("--start", type=lambda s: int(s, 0), default=0)
    parser.add_argument("--end", type=lambda s: int(s, 0), default=0x2000, help="exclusive")
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=0.3)
    parser.add_argument("--idle", type=float, default=0.005, help="inter-frame idle gap, s")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dump", action="store_true", help="print every readable word")
    parser.add_argument("--nonzero", action="store_true", help="with --dump, skip zero words")
    parser.add_argument("--out", default="probe_out")
    return anyio.run(run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
