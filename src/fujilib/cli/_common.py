"""Shared helpers for the ``fuji-*`` command-line tools.

Every command's ``main(argv=None) -> int`` returns 0 on success, 1 for a
library error (printed as ``error: ...``) and 2 for bad arguments.

The commands that talk to an analyzer take a serial port, or ``--fixture``
with a register bank (the JSON that ``fuji-decode --dump`` reads, or
``bench`` for the bundled bench bank), which a simulated analyzer then
answers from. Either way the command goes through ``open_device`` and the
same session as any program.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from contextlib import asynccontextmanager
from enum import Enum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import anyio

from fujilib.config import DEFAULTS
from fujilib.devices.factory import open_device
from fujilib.errors import FujiError, FujiValidationError
from fujilib.protocol.modbus.port import MAX_STATION, MIN_STATION
from fujilib.registry.channels import ChannelId, Gas, coerce_channel, coerce_gas
from fujilib.testing import BENCH_BANK_PATH, MockAnalyzer, load_bank, mock_transport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sized

    from fujilib.devices.analyzer import Analyzer
    from fujilib.transport.base import Transport

__all__ = [
    "add_open_args",
    "check_open_args",
    "open_from_args",
    "parse_gas",
    "render",
    "run_async_cli",
    "run_cli",
    "seconds",
    "station",
]

#: ``--fixture bench`` names the register bank bundled with fujilib.
BENCH_FIXTURE: Final = "bench"

#: The longest timeout ``anymodbus`` accepts, in seconds.
_MAX_SECONDS: Final = 60.0


def run_cli(body: Callable[[], int]) -> int:
    """Run a CLI body, turning a :class:`~fujilib.errors.FujiError` into exit code 1."""
    try:
        return body()
    except FujiError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


def run_async_cli(body: Callable[[], Awaitable[int]]) -> int:
    """Run an async CLI body on an event loop, as :func:`run_cli` does."""
    return run_cli(lambda: anyio.run(body))


# --- Opening an analyzer from the command line ------------------------------------------------


def parse_gas(text: str) -> tuple[ChannelId, Gas]:
    """Parse ``CHn=gas``, e.g. ``CH3=o2``.

    Raises:
        argparse.ArgumentTypeError: the text is not a channel and a gas.
    """
    channel, sep, gas = text.partition("=")
    try:
        if not sep:
            raise ValueError
        parsed = coerce_channel(channel), coerce_gas(gas)
        if parsed[1] is Gas.UNKNOWN:
            raise ValueError
    except (ValueError, FujiError):
        known = ", ".join(g.value for g in Gas if g is not Gas.UNKNOWN)
        msg = f"expected CHn=gas with gas one of {known}; got {text!r}"
        raise argparse.ArgumentTypeError(msg) from None
    return parsed


def station(text: str) -> int:
    """Parse a station number, 1-31.

    Raises:
        argparse.ArgumentTypeError: the text is not a station number.
    """
    try:
        number = int(text)
    except ValueError:
        number = 0
    if not MIN_STATION <= number <= MAX_STATION:
        msg = f"expected a station number {MIN_STATION}-{MAX_STATION}; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return number


def seconds(text: str) -> float:
    """Parse a timeout in seconds: more than 0, at most 60.

    Raises:
        argparse.ArgumentTypeError: the text is not such a number.
    """
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= _MAX_SECONDS):
        msg = f"expected seconds, more than 0 and at most {_MAX_SECONDS:g}; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def add_open_args(parser: argparse.ArgumentParser) -> None:
    """Add the arguments that say which analyzer to open."""
    parser.add_argument("port", nargs="?", help="Serial port, e.g. COM8 or /dev/ttyUSB0.")
    parser.add_argument(
        "--fixture",
        metavar="BANK",
        help=(
            "Answer from a register bank (JSON, as fuji-decode --dump reads) on a simulated "
            f"analyzer instead of a port; {BENCH_FIXTURE!r} is the bundled bench bank."
        ),
    )
    parser.add_argument(
        "--address", type=station, default=1, help="Station number, 1-31 (default: 1)."
    )
    parser.add_argument(
        "--timeout",
        type=seconds,
        default=DEFAULTS.request_timeout_s,
        help=f"Seconds to wait for each reply (default: {DEFAULTS.request_timeout_s:g}).",
    )
    parser.add_argument(
        "--gas",
        action="append",
        type=parse_gas,
        metavar="CHn=GAS",
        help="Assert a channel's gas (repeatable), e.g. --gas CH3=o2.",
    )


def check_open_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Exit with a usage error on a missing source or a channel asserted twice.

    Exactly one of a port and ``--fixture`` must be given.
    """
    if (args.port is None) == (args.fixture is None):
        parser.error("give either a serial port or --fixture")
    asserted: dict[ChannelId, Gas] = {}
    for channel, gas in args.gas or ():
        if asserted.setdefault(channel, gas) is not gas:
            parser.error(f"--gas asserts {channel.value} as both {asserted[channel]} and {gas}")


def _bank_path(fixture: str) -> Path:
    return BENCH_BANK_PATH if fixture == BENCH_FIXTURE else Path(fixture)


@asynccontextmanager
async def open_from_args(
    args: argparse.Namespace, *, identify: bool = True
) -> AsyncGenerator[Analyzer]:
    """Open the analyzer the arguments name, closing it on exit."""
    channel_map = dict(args.gas) if args.gas else None

    async def open_on(port: str | Transport) -> Analyzer:
        return await open_device(
            port,
            address=args.address,
            timeout=args.timeout,
            identify=identify,
            channel_map=channel_map,
        )

    if args.fixture is None:
        async with await open_on(args.port) as analyzer:
            yield analyzer
        return
    path = _bank_path(args.fixture)
    try:
        bank = await anyio.to_thread.run_sync(partial(load_bank, path, station=args.address))
    except (OSError, ValueError, TypeError) as exc:
        msg = f"cannot read the register bank {str(path)!r}: {exc}"
        raise FujiValidationError(msg) from exc
    async with (
        mock_transport(MockAnalyzer(bank), label=f"fixture:{path.name}") as (transport, _line),
        await open_on(transport) as analyzer,
    ):
        yield analyzer


# --- Output ---------------------------------------------------------------------------------


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex(" ").upper()
    return str(value)


def _is_nested(value: object) -> bool:
    return isinstance(value, dict | list) and len(cast("Sized", value)) > 0


def _text(value: object, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            if _is_nested(item):
                out.append(f"{pad}{key}:")
                _text(item, indent + 1, out)
            else:
                out.append(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            if _is_nested(item):
                out.append(f"{pad}-")
                _text(item, indent + 1, out)
            else:
                out.append(f"{pad}- {_scalar(item)}")
    else:
        out.append(f"{pad}{_scalar(value)}")


def _scalar(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Enum):
        return str(value.value)
    if _is_empty_container(value):
        return "none"
    return str(value)


def _is_empty_container(value: object) -> bool:
    return isinstance(value, dict | list) and len(cast("Sized", value)) == 0


def render(report: object, fmt: str) -> str:
    """Render a report as indented text or as JSON."""
    if fmt == "json":
        return json.dumps(report, indent=2, default=_json_default) + "\n"
    out: list[str] = []
    _text(report, 0, out)
    return "\n".join(out) + "\n"
