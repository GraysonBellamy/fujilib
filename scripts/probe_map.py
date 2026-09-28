"""Read-only register-map probe for a Fuji ZP-series gas analyzer.

Dumps every documented region, tests the region boundaries and the 64-word cap,
and checks how the analyzer answers function codes it does not implement. Prints a
decoded summary and writes the raw result to ``probe_out/`` as JSON.

Read-only by construction: see :mod:`_probe_common`. Register addresses and meanings
are from INZ-TN5A1190a-E chapter 7.

Usage::

    uv run --no-project --with anymodbus --with anyserial \
        python scripts/probe_map.py --port COM8 --address 1
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import anyio
from _probe_common import (
    FC_READ_COILS,
    FC_READ_DISCRETE,
    FC_READ_HOLDING,
    FC_READ_INPUT,
    Attempt,
    ReadOnlyStation,
    attempt,
    open_bus,
    to_int16,
    words_to_text,
)

UNITS = {0: "vol%", 1: "ppm", 2: "mg/m3", 3: "g/m3"}
ALARM_STATES = {0: "none", 1: "H", 2: "L", 3: "HH", 4: "LL"}
CAL_KINDS = {0: "Z1", 1: "S1", 2: "Z2", 3: "S2"}
EMPTY = 0xFFFF

#: (label, function code, first address, word count) — whole documented regions.
REGIONS: tuple[tuple[str, int, int, int], ...] = (
    ("input: measurement and status", FC_READ_INPUT, 0x0000, 0x00C2),
    ("input: fixed settings", FC_READ_INPUT, 0x0425, 0x0469 - 0x0425 + 1),
    ("input: type code 27-29 (fw >= 2.24)", FC_READ_INPUT, 0x047A, 3),
    ("holding: user settings", FC_READ_HOLDING, 0x0000, 0x00AC),
)

#: (label, function code, address, count, what the manual leads us to expect).
BOUNDARY_TESTS: tuple[tuple[str, int, int, int, str], ...] = (
    ("last input status word", FC_READ_INPUT, 0x00C1, 1, "ok"),
    ("first word past input status", FC_READ_INPUT, 0x00C2, 1, "exception 02"),
    ("read crossing end of input status", FC_READ_INPUT, 0x00C1, 2, "exception"),
    ("word before fixed settings", FC_READ_INPUT, 0x0424, 1, "exception 02"),
    ("first fixed-settings word", FC_READ_INPUT, 0x0425, 1, "ok"),
    ("last fixed-settings word", FC_READ_INPUT, 0x0469, 1, "ok"),
    ("word after fixed settings", FC_READ_INPUT, 0x046A, 1, "exception 02"),
    ("word before type 27-29", FC_READ_INPUT, 0x0479, 1, "exception 02"),
    ("word after type 27-29", FC_READ_INPUT, 0x047D, 1, "exception 02"),
    ("calibration log, first record", FC_READ_INPUT, 0x1000, 9, "ok if fw >= 2.24"),
    ("calibration log, last word", FC_READ_INPUT, 0x1707, 1, "ok if fw >= 2.24"),
    ("word after calibration log", FC_READ_INPUT, 0x1708, 1, "exception 02"),
    ("last holding word", FC_READ_HOLDING, 0x00AB, 1, "ok"),
    ("first word past holding", FC_READ_HOLDING, 0x00AC, 1, "exception 02"),
    ("command register read back", FC_READ_HOLDING, 0x07D0, 1, "exception 02 (write-only)"),
    ("input map read as holding", FC_READ_HOLDING, 0x0448, 1, "exception 02"),
    ("64 words (the documented cap)", FC_READ_INPUT, 0x0000, 64, "ok"),
    ("65 words (one over the cap)", FC_READ_INPUT, 0x0000, 65, "exception 03"),
    ("FC01 read coils (not implemented)", FC_READ_COILS, 0x0000, 1, "exception 01 or silent"),
    (
        "FC02 read discretes (not implemented)",
        FC_READ_DISCRETE,
        0x0000,
        1,
        "exception 01 or silent",
    ),
)


async def read_region(
    station: ReadOnlyStation, fc: int, first: int, count: int, chunk: int
) -> tuple[dict[int, int], list[Attempt]]:
    """Read ``count`` words from ``first`` in blocks of at most ``chunk``."""
    values: dict[int, int] = {}
    attempts: list[Attempt] = []
    address = first
    end = first + count
    while address < end:
        size = min(chunk, end - address)
        result = await attempt(station, fc, address, size)
        attempts.append(result)
        if result.ok:
            for offset, word in enumerate(result.words):
                values[address + offset] = word
        address += size
    return values, attempts


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def scaled(raw: int, decimals: int) -> str:
    return f"{raw / 10**decimals:.{decimals}f}" if 0 <= decimals <= 3 else f"{raw} (dp={decimals}?)"


def summarize_identity(inp: dict[int, int]) -> None:
    out("\n-- identity")
    type_words = tuple(inp.get(0x0448 + i, 0) for i in range(26))
    board_words = tuple(inp.get(0x0462 + i, 0) for i in range(8))
    out(f"type code (digits 1-26): {words_to_text(type_words)!r}")
    out(f"   raw: {' '.join(f'{w:04X}' for w in type_words)}")
    out(f"board code:              {words_to_text(board_words)!r}")
    if 0x047A in inp:
        tail = tuple(inp[0x047A + i] for i in range(3))
        out(f"type code (digits 27-29): {words_to_text(tail)!r}  -> firmware >= 2.24")
    else:
        out("type code digits 27-29: not readable -> firmware older than 2.24")


def summarize_ranges(inp: dict[int, int]) -> None:
    out("\n-- ranges (fixed settings)")
    out("ch  ranges  r1 full scale      r2 full scale")
    for ch in range(5):
        count = inp.get(0x0425 + ch, 0)
        cells = []
        for rng in range(2):
            idx = 2 * ch + rng
            unit = UNITS.get(inp.get(0x042A + idx, -1), "?")
            value = inp.get(0x0434 + idx, 0)
            decimals = inp.get(0x043E + idx, 0)
            cells.append(f"{scaled(value, decimals):>8s} {unit:<6s} dp={decimals}")
        out(f"{ch + 1:2d}  {count:6d}  {cells[0]}  {cells[1]}")


def summarize_readings(inp: dict[int, int]) -> None:
    out("\n-- live readings (Ch1-12)")
    out("ch   raw    dp  unit    value")
    for ch in range(12):
        base = 3 * ch
        raw = to_int16(inp.get(base, 0))
        decimals = inp.get(base + 1, 0)
        unit_code = inp.get(base + 2, 0)
        unit = UNITS.get(unit_code, f"?{unit_code}")
        out(f"{ch + 1:2d}  {raw:6d}  {decimals:2d}  {unit:<6s}  {scaled(raw, decimals)}")


def summarize_status(inp: dict[int, int]) -> None:
    out("\n-- status")
    out(f"peak count: {inp.get(0x0024)}   peak alarm: {inp.get(0x002F)}")
    out(f"current range Ch1-5 (0=r1, 1=r2): {[inp.get(0x0025 + i) for i in range(5)]}")
    alarms = [ALARM_STATES.get(inp.get(0x002A + i, -1), "?") for i in range(5)]
    alarms.append(ALARM_STATES.get(inp.get(0x00BE, -1), "?"))
    out(f"alarm 1-6 state: {alarms}")
    out(f"auto calibration running: {inp.get(0x0030)}")
    out(f"zero cal running Ch1-5: {[inp.get(0x0031 + i) for i in range(5)]}")
    out(f"span cal running Ch1-5: {[inp.get(0x0036 + i) for i in range(5)]}")
    out(f"instrument error: {inp.get(0x003B)}   calibration error: {inp.get(0x003C)}")
    out(f"active errors 1, 2, 3, 10: {[inp.get(0x0083 + i) for i in range(4)]}")
    for ch in range(5):
        errs = [inp.get(0x0087 + 6 * ch + i) for i in range(6)]
        flags = [inp.get(0x00A5 + 3 * ch + i) for i in range(3)]
        out(f"Ch{ch + 1}: errors 4-9 {errs}   auto-zero/auto-span/hold {flags}")
    out(
        f"display: screen={inp.get(0x00B4)} manual-cal={inp.get(0x00B5)} "
        f"top-ch={inp.get(0x00B6)} cursor-ch={inp.get(0x00BC)}"
    )
    unused = {a: inp[a] for a in (0xB7, 0xB8, 0xB9, 0xBA, 0xBB, 0xBD, 0xBF, 0xC0, 0xC1) if a in inp}
    out(f"'do not use' words: { {f'{a:04X}': v for a, v in unused.items()} }")


def summarize_error_log(inp: dict[int, int]) -> None:
    out("\n-- error log (newest first; stored as error No. - 1)")
    shown = 0
    for i in range(14):
        base = 0x003D + 5 * i
        number, day, hour, minute, target = (inp.get(base + k, EMPTY) for k in range(5))
        if number == EMPTY:
            continue
        shown += 1
        out(
            f"#{i + 1:2d}: error {to_int16(number) + 1:2d}  day {day:2d}  "
            f"{hour:02d}:{minute:02d}  channel {to_int16(target) + 1}"
        )
    if not shown:
        out("(empty)")


def summarize_holding(hold: dict[int, int]) -> None:
    if not hold:
        return
    out("\n-- selected settings (holding registers, raw values)")
    for ch in range(5):
        base = 4 * ch
        cal = [hold.get(base + i) for i in range(4)]
        out(f"Ch{ch + 1} calibration gas [r1 zero, r1 span, r2 zero, r2 span]: {cal}")
    out(f"key lock: {hold.get(0x0049)}   output hold: {hold.get(0x005C)}")
    out(f"response time Ch1-4, O2: {[hold.get(a) for a in (0x4B, 0x4D, 0x4F, 0x51, 0x53)]} s")
    out(f"range selection Ch1-5: {[hold.get(0x0069 + i) for i in range(5)]}")
    out(
        "range method Ch1-5 (0 manual, 1 remote, 2 auto): "
        f"{[hold.get(0x006E + i) for i in range(5)]}"
    )
    out(f"auto-calibration on/off: {hold.get(0x0047)}   auto-zero on/off: {hold.get(0x0067)}")
    out(f"O2 reference: {hold.get(0x005D)} %   O2 limit: {hold.get(0x009D)} %")
    undocumented = {f"{a:04X}": hold.get(a) for a in range(0x00A6, 0x00AC)}
    out(f"undocumented 40167-40172: {undocumented}")


def summarize_cal_record(words: tuple[int, ...]) -> None:
    if len(words) < 9:
        return
    channel, kind, low, high, deviation, month, day, hour, minute = words[:9]
    out(
        f"   channel={to_int16(channel)} kind={CAL_KINDS.get(kind, kind)} "
        f"count(low-word-first)={(high << 16) | low} count(high-word-first)={(low << 16) | high} "
        f"deviation={to_int16(deviation) / 10:.1f} %FS  at {month}/{day} {hour:02d}:{minute:02d}"
    )


async def run(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    report: dict[str, object] = {
        "probe": "probe_map",
        "port": args.port,
        "address": args.address,
        "started_utc": started.isoformat(),
        "inter_frame_idle_s": args.idle,
    }
    regions: dict[str, dict[int, int]] = {}
    attempts: list[dict[str, object]] = []
    boundaries: list[dict[str, object]] = []

    async with open_bus(
        args.port, timeout=args.timeout, idle=args.idle, retries=args.retries
    ) as bus:
        station = ReadOnlyStation(bus.slave(args.address))

        out("== region dump")
        for label, fc, first, count in REGIONS:
            values, tried = await read_region(station, fc, first, count, args.chunk)
            regions[label] = values
            for item in tried:
                attempts.append({"region": label, **asdict(item)})
                out(f"  {label:38s} {item.describe()}")

        out("\n== boundary and limit tests")
        for label, fc, address, count, expected in BOUNDARY_TESTS:
            result = await attempt(station, fc, address, count)
            boundaries.append({"test": label, "expected": expected, **asdict(result)})
            out(f"  {label:38s} {result.describe():46s} (expected: {expected})")
            if label.startswith("calibration log, first") and result.ok:
                summarize_cal_record(result.words)

    inputs: dict[int, int] = {}
    for label, values in regions.items():
        if label.startswith("input"):
            inputs.update(values)
    holding = regions.get("holding: user settings", {})

    summarize_identity(inputs)
    summarize_ranges(inputs)
    summarize_readings(inputs)
    summarize_status(inputs)
    summarize_error_log(inputs)
    summarize_holding(holding)

    report["finished_utc"] = datetime.now(UTC).isoformat()
    report["regions"] = {
        label: {f"{a:04X}": v for a, v in sorted(values.items())}
        for label, values in regions.items()
    }
    report["attempts"] = attempts
    report["boundary_tests"] = boundaries
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe_map_{started:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out(f"\nraw results written to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1, help="station number")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--idle", type=float, default=0.01, help="inter-frame idle gap, s")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--chunk", type=int, default=64, help="words per block read")
    parser.add_argument("--out", default="probe_out")
    return anyio.run(run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
