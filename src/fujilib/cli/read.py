"""``fuji-read`` — read an analyzer once: identity, a poll, status, metadata, logs.

Read-only: it sends read requests and nothing else. Choose what to read with
``--include`` (repeatable; the default is identity and one poll) or ``--all``.
A section the analyzer does not support, such as the calibration log before
firmware 2.24, is reported as unavailable rather than failing the command.

Examples::

    fuji-read COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2
    fuji-read COM8 --include metadata --include error-log --format json
    fuji-read --fixture bench --all
"""

from __future__ import annotations

import argparse
import sys
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.cli._common import (
    add_open_args,
    check_open_args,
    open_from_args,
    render,
    run_async_cli,
)
from fujilib.cli._report import (
    adc_report,
    calibration_log_report,
    clock_report,
    error_log_report,
    frame_report,
    info_report,
    metadata_report,
    ranges_report,
    snapshot_report,
    status_report,
)
from fujilib.errors import FujiCapabilityError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from fujilib.devices.analyzer import Analyzer

__all__ = ["SECTIONS", "main"]


async def _identity(anz: Analyzer) -> object:
    info = anz.info
    return info_report(info) if info is not None else None


async def _poll(anz: Analyzer) -> object:
    return frame_report(await anz.poll())


async def _status(anz: Analyzer) -> object:
    return status_report(await anz.status())


async def _metadata(anz: Analyzer) -> object:
    meta = await anz.read_metadata()
    return {
        **metadata_report(meta),
        "current_range": {c.value: r for c, r in meta.current_range.items()},
        "clock": meta.clock.isoformat(sep=" ") if meta.clock else None,
    }


async def _ranges(anz: Analyzer) -> object:
    return ranges_report(await anz.read_ranges())


async def _error_log(anz: Analyzer) -> object:
    return error_log_report(await anz.read_error_log())


async def _calibration_log(anz: Analyzer) -> object:
    return calibration_log_report(await anz.read_calibration_log())


async def _clock(anz: Analyzer) -> object:
    return clock_report(await anz.read_clock())


async def _adc(anz: Analyzer) -> object:
    return adc_report(await anz.read_adc())


async def _snapshot(anz: Analyzer) -> object:
    return snapshot_report(await anz.snapshot())


#: What ``--include`` can name, in report order.
SECTIONS: Final[Mapping[str, Callable[[Analyzer], Awaitable[object]]]] = MappingProxyType(
    {
        "identity": _identity,
        "poll": _poll,
        "status": _status,
        "metadata": _metadata,
        "ranges": _ranges,
        "error-log": _error_log,
        "calibration-log": _calibration_log,
        "clock": _clock,
        "adc": _adc,
        "snapshot": _snapshot,
    }
)
_DEFAULT: Final = ("identity", "poll")


async def _read(args: argparse.Namespace) -> int:
    wanted = set(SECTIONS) if args.all else set(args.include or _DEFAULT)
    report: dict[str, object] = {}
    async with open_from_args(args) as anz:
        for name, read in SECTIONS.items():
            if name not in wanted:
                continue
            try:
                report[name] = await read(anz)
            except FujiCapabilityError as exc:
                report[name] = f"not available: {exc}"
    sys.stdout.write(render(report, args.format))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-read",
        description="Read a Fuji ZP-series analyzer once. Sends read requests only.",
    )
    add_open_args(parser)
    parser.add_argument(
        "--include",
        action="append",
        choices=tuple(SECTIONS),
        metavar="SECTION",
        help=f"What to read (repeatable): {', '.join(SECTIONS)}. Default: identity and poll.",
    )
    parser.add_argument("--all", action="store_true", help="Read every section.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code (0 ok, 1 library error, 2 bad arguments)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    check_open_args(parser, args)
    return run_async_cli(lambda: _read(args))


if __name__ == "__main__":
    raise SystemExit(main())
