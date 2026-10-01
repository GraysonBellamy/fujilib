"""Bench probe of what the response-time setting does to each value the analyzer reports.

**With ``--confirm`` this probe writes to the analyzer.** It sets the response
time of the live components (``response_time.ndir1``, ``response_time.ndir2``,
``response_time.o2``) to each value of ``--stages`` in turn, 0 (the filter
off) included, and puts back what it found before it ends. It needs the
owner's authorization for the session. Without ``--confirm`` it sends reads
only, at the setting it finds.

Through every stage it reads, as fast as the link allows:

- the Ch1-Ch3 concentration registers (0000h-0008h);
- A/D values No. 0-4 (03EFh-03F8h), which follow the response time;
- the unsmoothed CO2 and CO detector counts (046Ah-0471h), which do not.

It records a stage at the setting it finds, one for each of ``--stages``, and
one more once the setting is back.

**Gas at rest** (the default). The filter shows in the noise: how far each
value scatters, how often it changes, and how alike consecutive reads are.
Each stage's first ``--settle`` seconds are recorded but left out of its
figures.

**A gas change** (``--change-at``, with ``--back-at``). In every stage the
probe tells the operator, with a line and a sound, when to change the gas at
the inlet and when to change it back, so each setting sees the same two steps.
The times of the cues are recorded with the reads; the curves are compared
afterwards, the filtered values against the unsmoothed counts of the same
step.

Every read goes to ``probe_out/probe_response_<time>.jsonl`` and the figures,
with the arguments and package versions, to the ``.json`` beside it (design
§11, "Evidence").

Usage::

    uv run python scripts/probe_response.py --port COM8
    uv run python scripts/probe_response.py --port COM8 --stages 1,0,1,0 --confirm
    uv run python scripts/probe_response.py --port COM8 --stages 0,15 --confirm \
        --seconds 150 --change-at 20 --back-at 80
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from fujilib import FujiError, open_device
from fujilib.protocol.modbus.read_plan import BlockRead

if TYPE_CHECKING:
    from typing import TextIO

    from fujilib import Analyzer

OUT = Path(__file__).resolve().parent.parent / "probe_out"
SLOTS = ("response_time.ndir2", "response_time.ndir1", "response_time.o2")
FC_READ_INPUT = 4
READINGS = BlockRead(FC_READ_INPUT, 0x0000, 9)
ADC = BlockRead(FC_READ_INPUT, 0x03EF, 10)
COUNTS = BlockRead(FC_READ_INPUT, 0x046A, 8)
SERIES = (
    "ch1_reading",
    "ch2_reading",
    "ch3_reading",
    "adc0_co2",
    "adc1_co",
    "adc4_o2",
    "count_co2",
    "count_co",
)
#: Seconds before a cue at which the operator is told it is coming.
WARNING = 5.0


def _int16(word: int) -> int:
    return word - 0x10000 if word & 0x8000 else word


def _long(words: tuple[int, ...], index: int) -> int:
    return words[index] | words[index + 1] << 16


def _sound() -> None:
    if sys.platform == "win32":
        import winsound  # noqa: PLC0415

        winsound.MessageBeep()
    else:
        print("\a", end="", flush=True)


async def _sample(anz: Analyzer) -> dict[str, int]:
    client = anz.session._client
    readings = (await client.read(READINGS, command="probe_response")).words
    adc = (await client.read(ADC, command="probe_response")).words
    counts = (await client.read(COUNTS, command="probe_response")).words
    return {
        "ch1_reading": _int16(readings[0]),
        "ch2_reading": _int16(readings[3]),
        "ch3_reading": _int16(readings[6]),
        "adc0_co2": _long(adc, 0),
        "adc1_co": _long(adc, 2),
        "adc4_o2": _long(adc, 8),
        "count_co2": _long(counts, 0),
        "count_co": _long(counts, 4),
    }


async def _settings(anz: Analyzer) -> dict[str, int]:
    return {name: int((await anz.read_parameter(name)).raw) for name in SLOTS}


async def _set_all(anz: Analyzer, value: int) -> None:
    """Write ``value`` to each slot, checking after each that readings still come."""
    for name in SLOTS:
        await anz.write_parameter(name, value, confirm=True)
        await _sample(anz)


async def _restore(anz: Analyzer, before: dict[str, int]) -> dict[str, int]:
    for attempt in range(5):
        try:
            for name, value in before.items():
                await anz.write_parameter(name, value, confirm=True)
            return await _settings(anz)
        except FujiError as exc:
            print(f"restore attempt {attempt + 1} failed: {exc}", flush=True)
            await anyio.sleep(2.0)
    msg = f"COULD NOT RESTORE {before}: set the response times at the panel"
    raise SystemExit(msg)


def _figures(times: list[float], values: list[int]) -> dict[str, Any]:
    n = len(values)
    if n < 3:
        return {"n": n}
    mean = statistics.fmean(values)
    changes = [i for i in range(1, n) if values[i] != values[i - 1]]
    gaps = [times[b] - times[a] for a, b in pairwise(changes)]
    variance = sum((v - mean) ** 2 for v in values)
    lag1 = (
        sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n)) / variance
        if variance
        else None
    )
    return {
        "n": n,
        "mean": round(mean, 3),
        "stdev": round(statistics.stdev(values), 3),
        "min": min(values),
        "max": max(values),
        "changed_fraction": round(len(changes) / (n - 1), 3),
        "lag1_autocorrelation": None if lag1 is None else round(lag1, 3),
        "shortest_gap_between_changes_ms": round(min(gaps) * 1000, 1) if gaps else None,
        "median_gap_between_changes_ms": round(statistics.median(gaps) * 1000, 1) if gaps else None,
    }


def _cues(args: argparse.Namespace) -> list[tuple[float, str, str]]:
    """The stage's cues, in order: (seconds into the stage, kind, what the operator reads)."""
    if args.change_at is None:
        return []
    marks = [(args.change_at, "change", ">>> CHANGE THE GAS NOW", "change the gas")]
    if args.back_at is not None:
        marks.append((args.back_at, "back", "<<< CHANGE IT BACK NOW", "change it back"))
    cues: list[tuple[float, str, str]] = []
    for at, kind, text, coming in marks:
        cues.append((at - WARNING, f"{kind}_warning", f"    {coming} in {WARNING:.0f} s"))
        cues.append((at, kind, text))
    return cues


async def _stage(
    anz: Analyzer, log: TextIO, label: str, args: argparse.Namespace
) -> dict[str, Any]:
    cues = _cues(args)
    settle = 0.0 if cues else args.settle
    start = time.monotonic()
    times: list[float] = []
    rows: list[dict[str, int]] = []
    given: dict[str, float] = {}
    failed = 0
    print(f"\n{label}: recording for {args.seconds:.0f} s", flush=True)
    while (elapsed := time.monotonic() - start) < args.seconds:
        if cues and elapsed >= cues[0][0]:
            _, kind, text = cues.pop(0)
            print(text, flush=True)
            if not kind.endswith("_warning"):
                _sound()
                given[kind] = round(elapsed, 4)
                log.write(json.dumps({"stage": label, "t": given[kind], "cue": kind}) + "\n")
        try:
            row = await _sample(anz)
        except FujiError as exc:
            failed += 1
            log.write(
                json.dumps({"stage": label, "t": round(elapsed, 4), "error": str(exc)}) + "\n"
            )
            continue
        at = time.monotonic() - start
        log.write(json.dumps({"stage": label, "t": round(at, 4), **row}) + "\n")
        log.flush()
        if at >= settle:
            times.append(at)
            rows.append(row)
    figures = {name: _figures(times, [row[name] for row in rows]) for name in SERIES}
    print(f"{label}: {len(rows)} samples, {failed} failed reads", flush=True)
    for name, f in figures.items():
        if f["n"] >= 3 and not given:
            print(
                f"  {name:<12} mean {f['mean']:>10} sd {f['stdev']:>7} "
                f"changed {f['changed_fraction']:>5} lag1 {f['lag1_autocorrelation']}",
                flush=True,
            )
    return {"stage": label, "samples": len(rows), "failed": failed, "cues": given, **figures}


async def main_async(args: argparse.Namespace) -> Path:
    started = datetime.now(UTC)
    OUT.mkdir(exist_ok=True)
    stem = OUT / f"probe_response_{started:%Y%m%dT%H%M%SZ}"
    stages: list[dict[str, Any]] = []
    async with await open_device(args.port, address=args.address) as anz:
        serial = anz.info.serial_number if anz.info else None
        before = await _settings(anz)
        print(f"analyzer {serial}; response times before: {before}", flush=True)
        restored: dict[str, int] | None = None
        with stem.with_suffix(".jsonl").open("w", encoding="utf-8") as log:
            try:
                stages.append(await _stage(anz, log, "before", args))
                for index, value in enumerate(args.stages, start=1):
                    await _set_all(anz, value)
                    stages.append(await _stage(anz, log, f"{index}:{value}s", args))
                if args.stages:
                    restored = await _restore(anz, before)
                    stages.append(await _stage(anz, log, "restored", args))
            finally:
                if args.stages and restored is None:
                    restored = await _restore(anz, before)
        print(f"\nresponse times after: {restored or before}", flush=True)
    document = {
        "probe": "probe_response",
        "arguments": {
            "port": args.port,
            "address": args.address,
            "stages": args.stages,
            "seconds": args.seconds,
            "settle": args.settle,
            "change_at": args.change_at,
            "back_at": args.back_at,
        },
        "started_at": started.isoformat(),
        "versions": {
            "python": platform.python_version(),
            **{p: version(p) for p in ("fujilib", "anymodbus", "anyserial", "anyio")},
        },
        "serial_number": serial,
        "before": before,
        "after": restored or before,
        "stages": stages,
    }
    path = stem.with_suffix(".json")
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1)
    parser.add_argument(
        "--stages",
        type=lambda text: [int(part) for part in text.split(",")],
        default=[],
        help="Response times to set in turn, in seconds, e.g. 1,0,1,0. Needs --confirm.",
    )
    parser.add_argument("--seconds", type=float, default=60.0, help="Length of each stage.")
    parser.add_argument(
        "--settle",
        type=float,
        default=20.0,
        help="Seconds of each stage left out of its figures, with the gas at rest.",
    )
    parser.add_argument(
        "--change-at", type=float, help="Seconds into each stage at which to change the gas."
    )
    parser.add_argument(
        "--back-at", type=float, help="Seconds into each stage at which to change it back."
    )
    parser.add_argument("--confirm", action="store_true", help="Required with --stages: it writes.")
    args = parser.parse_args()
    if args.stages and not args.confirm:
        parser.error("--stages writes to the analyzer; pass --confirm")
    if any(not 0 <= value <= 60 for value in args.stages):
        parser.error("stages must be 0..60 s")
    if args.back_at is not None and (args.change_at is None or args.back_at <= args.change_at):
        parser.error("--back-at needs an earlier --change-at")
    if args.change_at is not None and not WARNING <= args.change_at < args.seconds:
        parser.error(f"--change-at must be {WARNING:.0f} s or more into the stage, and inside it")
    path = anyio.run(main_async, args)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
