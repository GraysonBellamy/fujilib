"""Read-only link-timing probe for a Fuji ZP-series gas analyzer.

Two modes, both with **no retries**. Nothing is printed inside a measuring loop: work
between requests adds gap and hides exactly the failures this measures (the lesson
recorded for servomexlib).

``--mode sweep`` (the original): for each ``anymodbus`` inter-frame idle, a tight loop of
identical reads; reports failure rate and latency. Its gaps are counted by
``anymodbus`` and rounded up by the Windows timer, so it cannot measure small gaps
(design §2.4).

``--mode pairs``: trials of two reads, "first" then "second", each a normal read or
one that draws exception 02, in all four pairings, in randomized order. The gap
before the second read is measured from the moment the first read returned to the
moment ``anymodbus`` logs the second frame for sending:

- ``--gap-source busywait``: ``anymodbus`` idle is 0 and the probe busy-waits each gap
  in ``--gaps-ms``. This measures what the analyzer needs.
- ``--gap-source anymodbus``: the probe adds no wait and leaves the gap to
  ``anymodbus``'s own idle (``--idle-ms``). This checks the library's behaviour.

Every trial is written to the output file, not only the counts (design §10).

Read-only by construction: see :mod:`_probe_common`.

Usage::

    uv run --no-project --with anymodbus --with anyserial \
        python scripts/probe_link.py --port COM8 --address 1 --mode pairs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import random
import statistics
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import anyio
from _probe_common import FC_READ_INPUT, ReadOnlyStation, attempt, open_bus

#: Pairs mode: a valid input register, and the first address past the map, which
#: answers exception 02 (design §2.3).
KINDS = {"normal": (0x0000, "ok"), "exception": (0x00C2, "exception")}
PAIRINGS = [(first, second) for first in KINDS for second in KINDS]
#: Pairs mode: idle before each trial's first read, so trials do not affect each other.
TRIAL_SETTLE_S = 0.010


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return float("nan")
    index = min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


async def measure(
    port: str,
    address: int,
    *,
    idle: float,
    count: int,
    words: int,
    timeout: float,
    register: int = 0x0000,
    expect: str = "ok",
) -> dict[str, object]:
    """Run ``count`` identical reads and tally outcomes.

    ``expect`` is the outcome that counts as success: ``"ok"`` for a valid register,
    ``"exception"`` when ``register`` is deliberately outside the map.
    """
    outcomes: dict[str, int] = {}
    latencies: list[float] = []
    async with open_bus(port, timeout=timeout, idle=idle, retries=0) as bus:
        station = ReadOnlyStation(bus.slave(address))
        # One untimed request so adapter warm-up is not counted.
        await attempt(station, FC_READ_INPUT, 0x0000, 1)
        results = [await attempt(station, FC_READ_INPUT, register, words) for _ in range(count)]
    for result in results:
        key = result.outcome if result.outcome != "error" else result.detail.split(":")[0]
        outcomes[key] = outcomes.get(key, 0) + 1
        if result.outcome == expect:
            # The call includes the enforced idle gap; subtract it for the round trip.
            latencies.append(max(result.elapsed_ms - idle * 1000.0, 0.0))
    latencies.sort()
    failed = count - outcomes.get(expect, 0)
    return {
        "idle_ms": idle * 1000.0,
        "register": register,
        "expect": expect,
        "words": words,
        "requests": count,
        "failed": failed,
        "failure_rate": failed / count,
        "outcomes": outcomes,
        "latency_ms": {
            "min": latencies[0] if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "p95": percentile(latencies, 0.95),
            "max": latencies[-1] if latencies else None,
        },
    }


async def run(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    gaps = [float(g) / 1000.0 for g in (args.gaps_ms or "0,1.75,2.5,5,10,20,50").split(",")]
    rows: list[dict[str, object]] = []
    out(
        "idle ms  words  requests  failed   rate    "
        "latency ms (min / median / p95 / max)   outcomes"
    )
    sizes = (1,) if args.expect == "exception" else (1, 64)
    for words in sizes:
        for idle in gaps:
            row = await measure(
                args.port,
                args.address,
                idle=idle,
                count=args.count,
                words=words,
                timeout=args.timeout,
                register=args.register,
                expect=args.expect,
            )
            rows.append(row)
            lat = row["latency_ms"]
            assert isinstance(lat, dict)
            out(
                f"{row['idle_ms']:7.2f}  {words:5d}  {row['requests']:8d}  {row['failed']:6d}  "
                f"{100 * float(row['failure_rate']):5.2f}%   "  # type: ignore[arg-type]
                f"{lat['min']:6.1f} / {lat['median']:6.1f} / "
                f"{lat['p95']:6.1f} / {lat['max']:6.1f}     "
                f"{row['outcomes']}"
            )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe_link_{started:%Y%m%dT%H%M%SZ}.json"
    path.write_text(
        json.dumps(
            {
                "probe": "probe_link",
                "port": args.port,
                "address": args.address,
                "started_utc": started.isoformat(),
                "rows": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    out(f"\nraw results written to {path}")
    return 0


class TxClock(logging.Handler):
    """Notes when ``anymodbus`` logs an outgoing frame: after its idle wait, just before send."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.last_tx = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        if isinstance(record.msg, str) and record.msg.startswith("tx"):
            self.last_tx = time.perf_counter()


def spin_until(deadline: float) -> None:
    """Busy-wait; ``anyio.sleep`` rounds short waits up to about 16 ms on Windows."""
    while time.perf_counter() < deadline:
        pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run_pairs(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    busywait = args.gap_source == "busywait"
    gaps = [float(g) for g in (args.gaps_ms or "0,1,2,5").split(",")] if busywait else [None]
    plan = [(a, b, g) for a, b in PAIRINGS for g in gaps for _ in range(args.count)]
    random.Random(seed).shuffle(plan)  # noqa: S311 - reproducible trial order, not security

    clock = TxClock()
    bus_logger = logging.getLogger("anymodbus.bus")
    bus_logger.addHandler(clock)
    bus_logger.setLevel(logging.DEBUG)
    bus_logger.propagate = False

    idle_s = 0.0 if busywait else args.idle_ms / 1000.0
    trials: list[dict[str, object]] = []
    async with open_bus(args.port, timeout=args.timeout, idle=idle_s, retries=0) as bus:
        station = ReadOnlyStation(bus.slave(args.address))
        await attempt(station, FC_READ_INPUT, 0x0000, 1)  # adapter warm-up, not counted
        last_end = time.perf_counter()
        for first, second, gap_ms in plan:
            spin_until(last_end + TRIAL_SETTLE_S)
            a = await attempt(station, FC_READ_INPUT, KINDS[first][0], 1)
            a_end = time.perf_counter()
            if gap_ms is not None:
                spin_until(a_end + gap_ms / 1000.0)
            b = await attempt(station, FC_READ_INPUT, KINDS[second][0], 1)
            last_end = time.perf_counter()
            trial: dict[str, object] = {
                "first": first,
                "second": second,
                "target_gap_ms": gap_ms,
                "gap_ms": round((clock.last_tx - a_end) * 1000.0, 3),
                "first_outcome": a.outcome,
                "second_outcome": b.outcome,
                "second_round_trip_ms": round((last_end - clock.last_tx) * 1000.0, 3),
            }
            for label, result in (("first", a), ("second", b)):
                if result.detail:
                    trial[f"{label}_detail"] = result.detail
                if result.exception_code not in (None, 2):
                    trial[f"{label}_exception_code"] = result.exception_code
            trials.append(trial)
    ended = datetime.now(UTC)

    rows = summarize(trials)
    out(f"gap source: {args.gap_source}" + ("" if busywait else f", idle {args.idle_ms} ms"))
    out("first      second     target  trials  first-bad  failed  gap ms (min / median / max)")
    for row in rows:
        target = "-" if row["target_gap_ms"] is None else f"{row['target_gap_ms']:g}"
        g = row["gap_ms"]
        assert isinstance(g, dict)
        out(
            f"{row['first']:<10} {row['second']:<10} {target:>6}  {row['trials']:6d}  "
            f"{row['first_bad']:9d}  {row['failed']:6d}  "
            f"{g['min']:6.2f} / {g['median']:6.2f} / {g['max']:6.2f}  {row['failures'] or ''}"
        )

    scripts = Path(__file__).resolve().parent
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe_link_pairs_{args.gap_source}_{started:%Y%m%dT%H%M%SZ}.json"
    meta = {
        "probe": "probe_link --mode pairs",
        "gap_source": args.gap_source,
        "port": args.port,
        "address": args.address,
        "request_timeout_s": args.timeout,
        "retries": 0,
        "anymodbus_idle_ms": idle_s * 1000.0,
        "busywait_gaps_ms": gaps if busywait else None,
        "trials_per_cell": args.count,
        "trial_settle_ms": TRIAL_SETTLE_S * 1000.0,
        "registers": {kind: f"FC04 {reg:04X}h x1 -> {want}" for kind, (reg, want) in KINDS.items()},
        "gap_measured": "first read returned -> anymodbus 'tx' log of the second frame",
        "seed": seed,
        "started_utc": started.isoformat(),
        "ended_utc": ended.isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {p: version(p) for p in ("anymodbus", "anyserial", "anyio")},
        "sha256": {name: _sha256(scripts / name) for name in ("probe_link.py", "_probe_common.py")},
    }
    path.write_text(
        json.dumps({"meta": meta, "summary": rows, "trials": trials}, indent=1),
        encoding="utf-8",
    )
    out(f"\n{len(trials)} trials written to {path}")
    return 0


def summarize(trials: list[dict[str, object]]) -> list[dict[str, object]]:
    """Per (first, second, target gap): trials, first reads that misbehaved, failures."""
    cells: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for trial in trials:
        key = (trial["first"], trial["second"], trial["target_gap_ms"])
        cells.setdefault(key, []).append(trial)
    rows: list[dict[str, object]] = []
    for (first, second, target), group in sorted(
        cells.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2] or 0.0)
    ):
        want_first = KINDS[str(first)][1]
        want_second = KINDS[str(second)][1]
        valid = [t for t in group if t["first_outcome"] == want_first]
        failures: dict[str, int] = {}
        for t in valid:
            if t["second_outcome"] != want_second:
                failures[str(t["second_outcome"])] = failures.get(str(t["second_outcome"]), 0) + 1
        gaps = sorted(float(t["gap_ms"]) for t in valid)  # type: ignore[arg-type]
        rows.append(
            {
                "first": first,
                "second": second,
                "target_gap_ms": target,
                "trials": len(group),
                "first_bad": len(group) - len(valid),
                "failed": sum(failures.values()),
                "failures": failures,
                "gap_ms": {
                    "min": gaps[0] if gaps else float("nan"),
                    "median": statistics.median(gaps) if gaps else float("nan"),
                    "max": gaps[-1] if gaps else float("nan"),
                },
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1, help="station number")
    parser.add_argument("--mode", choices=("sweep", "pairs"), default="sweep")
    parser.add_argument(
        "--gaps-ms",
        default=None,
        help="sweep: anymodbus idle gaps (default 0,1.75,2.5,5,10,20,50); "
        "pairs with busywait: busy-waited gaps (default 0,1,2,5)",
    )
    parser.add_argument(
        "--count", type=int, default=300, help="sweep: requests per setting; pairs: trials per cell"
    )
    parser.add_argument("--gap-source", choices=("busywait", "anymodbus"), default="busywait")
    parser.add_argument("--idle-ms", type=float, default=5.0, help="pairs with anymodbus: idle")
    parser.add_argument("--seed", type=int, default=None, help="pairs: trial-order seed")
    parser.add_argument("--timeout", type=float, default=0.3)
    parser.add_argument("--register", type=lambda s: int(s, 0), default=0x0000)
    parser.add_argument(
        "--expect",
        choices=("ok", "exception"),
        default="ok",
        help='use "exception" with a --register outside the map',
    )
    parser.add_argument("--out", default="probe_out")
    args = parser.parse_args(argv)
    return anyio.run(run_pairs if args.mode == "pairs" else run, args)


if __name__ == "__main__":
    raise SystemExit(main())
