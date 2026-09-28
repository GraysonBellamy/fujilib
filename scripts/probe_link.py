"""Read-only link-timing probe for a Fuji ZP-series gas analyzer.

For each inter-frame idle gap, issues a tight loop of reads with **no retries** and
reports the failure rate and the round-trip latency. Nothing is printed inside the
loop: work between requests adds gap and hides exactly the failures this measures
(the lesson recorded for servomexlib).

Read-only by construction: see :mod:`_probe_common`.

Usage::

    uv run --no-project --with anymodbus --with anyserial \
        python scripts/probe_link.py --port COM8 --address 1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
from _probe_common import FC_READ_INPUT, ReadOnlyStation, attempt, open_bus


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
    gaps = [float(g) / 1000.0 for g in args.gaps_ms.split(",")]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1, help="station number")
    parser.add_argument("--gaps-ms", default="0,1.75,2.5,5,10,20,50", help="idle gaps to test")
    parser.add_argument("--count", type=int, default=300, help="requests per setting")
    parser.add_argument("--timeout", type=float, default=0.3)
    parser.add_argument("--register", type=lambda s: int(s, 0), default=0x0000)
    parser.add_argument(
        "--expect",
        choices=("ok", "exception"),
        default="ok",
        help='use "exception" with a --register outside the map',
    )
    parser.add_argument("--out", default="probe_out")
    return anyio.run(run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
