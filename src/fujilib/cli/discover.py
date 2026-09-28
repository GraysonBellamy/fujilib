"""``fuji-discover`` — find analyzers on serial ports; read-only.

Each station gets one read of type-code digits 1-3; a station that answers as
a ZP analyzer is then identified. Name the ports to scan: every port listed
receives the probe frames, so ``--all-ports`` (every serial port of the host)
must be asked for explicitly.

Exit code 0 when an analyzer was found, 2 when none was (as the sibling
libraries' discovery commands do) or the arguments are wrong, 1 for an error.

Examples::

    fuji-discover COM8
    fuji-discover COM8 --addresses 1-31 --probe-timeout 0.2
    fuji-discover --all-ports --no-identify --format json
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from fujilib.cli._common import render, run_async_cli, seconds
from fujilib.cli._report import info_report
from fujilib.devices.discovery import find_devices, summarize_discovery
from fujilib.protocol.modbus.port import MAX_STATION, MIN_STATION

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib.devices.discovery import DiscoveryResult, DiscoverySummary

__all__ = ["main", "parse_addresses"]

#: Exit code when the scan found no analyzer.
NOTHING_FOUND = 2


def parse_addresses(text: str) -> tuple[int, ...]:
    """Parse station numbers such as ``1``, ``1,2,5``, ``1-31`` or ``1-3,7``.

    Raises:
        argparse.ArgumentTypeError: the text is not station numbers 1-31.
    """
    stations: list[int] = []
    try:
        for part in text.split(","):
            first, sep, last = part.strip().partition("-")
            low = int(first)
            high = int(last) if sep else low
            if not MIN_STATION <= low <= high <= MAX_STATION:
                raise ValueError
            stations.extend(range(low, high + 1))
    except ValueError:
        msg = (
            f"expected station numbers {MIN_STATION}-{MAX_STATION} like 1,2,5 or 1-31; got {text!r}"
        )
        raise argparse.ArgumentTypeError(msg) from None
    return tuple(dict.fromkeys(stations))


def _result(result: DiscoveryResult) -> dict[str, object]:
    return {
        "port": result.port,
        "address": result.address,
        "ok": result.ok,
        "model": result.model,
        "error": str(result.error) if result.error is not None else None,
        "elapsed_s": round(result.elapsed_s, 3),
        "device": info_report(result.device_info) if result.device_info else None,
    }


def _summary(summary: DiscoverySummary) -> dict[str, object]:
    return {
        "port": summary.port,
        "found": list(summary.addresses),
        "probed": summary.probed,
        "error": str(summary.error) if summary.error is not None else None,
    }


def _line(result: DiscoveryResult) -> str:
    where = f"{result.port} station {result.address}"
    if result.ok:
        info = result.device_info
        found = f"{result.model} {info.serial_number}" if info is not None else str(result.model)
        if result.error is not None:
            found += f" (identify failed: {result.error})"
        return f"{where}: {found}"
    return f"{where}: - {result.error}"


async def _discover(args: argparse.Namespace) -> int:
    results = await find_devices(
        ports=None if args.all_ports else args.ports,
        addresses=args.addresses,
        per_probe_timeout_s=args.probe_timeout,
        identify=not args.no_identify,
    )
    summaries = summarize_discovery(results)
    if args.format == "json":
        report = {
            "results": [_result(r) for r in results],
            "ports": [_summary(s) for s in summaries],
        }
        sys.stdout.write(render(report, "json"))
    else:
        found = sum(len(s.addresses) for s in summaries)
        lines = [_line(r) for r in results]
        lines.append(f"{found} analyzer(s) found on {len(summaries)} port(s)")
        sys.stdout.write("\n".join(lines) + "\n")
    return 0 if any(s.ok for s in summaries) else NOTHING_FOUND


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-discover",
        description="Find Fuji ZP-series analyzers on serial ports. Sends read requests only.",
    )
    parser.add_argument("ports", nargs="*", metavar="PORT", help="Serial ports to scan.")
    parser.add_argument(
        "--all-ports",
        action="store_true",
        help="Scan every serial port of the host; each receives the probe frames.",
    )
    parser.add_argument(
        "--addresses",
        type=parse_addresses,
        default=(1,),
        help="Station numbers to probe, e.g. 1,2,5 or 1-31 (default: 1).",
    )
    parser.add_argument(
        "--probe-timeout",
        type=seconds,
        default=0.3,
        help="Seconds to wait for each station's reply (default: 0.3).",
    )
    parser.add_argument(
        "--no-identify", action="store_true", help="Report the model only; skip identification."
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: 0 found, 1 library error, 2 nothing found or bad arguments."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if bool(args.ports) == args.all_ports:
        parser.error("name the ports to scan, or give --all-ports, not both")
    return run_async_cli(lambda: _discover(args))


if __name__ == "__main__":
    raise SystemExit(main())
