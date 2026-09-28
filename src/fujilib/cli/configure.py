"""``fuji-configure`` — the analyzer's settings as a document.

``dump`` reads every holding register and writes them as JSON, keyed by
register name (as in ``docs/registers.md``), never by address. Each entry
has the decoded ``value``, the ``raw`` register value, its ``unit``, and
the register's ``access``, ``safety`` tier and ``evidence``. The document
carries a ``format`` version and the analyzer's identity, so it can be
compared with another analyzer's or a later dump.

Alarm limits are scaled only when ``--alarm-target`` names each alarm's
channel, because the target register's encoding is contested (design §5.2).

Examples::

    fuji-configure dump COM8 --out zpa-settings.json
    fuji-configure dump --fixture bench --format text
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import anyio

from fujilib.cli._common import (
    add_open_args,
    check_open_args,
    open_from_args,
    render,
    run_async_cli,
)
from fujilib.cli._report import settings_report
from fujilib.errors import FujiConfigurationError, FujiError
from fujilib.registry.channels import ChannelId, coerce_channel
from fujilib.version import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["SETTINGS_FORMAT", "main"]

#: The version of the settings document's layout.
SETTINGS_FORMAT: Final = "fujilib-settings/1"

#: Alarms 1-6, whose target channels ``--alarm-target`` names (design §5.2).
_ALARMS: Final = range(1, 7)


def _parse_target(text: str) -> tuple[int, ChannelId]:
    alarm, sep, channel = text.partition("=")
    try:
        if not sep or int(alarm) not in _ALARMS:
            raise ValueError
        return int(alarm), coerce_channel(channel)
    except (ValueError, FujiError):
        msg = f"expected N=CHn, an alarm number 1-6 and its target channel; got {text!r}"
        raise argparse.ArgumentTypeError(msg) from None


async def _dump(args: argparse.Namespace) -> int:
    targets = dict(args.alarm_target) if args.alarm_target else None
    async with open_from_args(args) as anz:
        settings = settings_report(await anz.read_settings(alarm_targets=targets))
        info = anz.info
        port, address = anz.port, anz.address
    document = {
        "format": SETTINGS_FORMAT,
        "fujilib_version": __version__,
        "captured_at": datetime.now(UTC).isoformat(),
        "analyzer": {
            "model": info.model if info is not None else None,
            "serial_number": info.serial_number if info is not None else None,
            "type_code": info.type_code.raw if info is not None else None,
            "port": port,
            "address": address,
        },
        "settings": settings,
    }
    if args.out is None:
        sys.stdout.write(render(document, args.format))
    else:
        try:
            await anyio.Path(args.out).write_text(render(document, "json"), encoding="utf-8")
        except OSError as exc:
            msg = f"cannot write {args.out}: {exc}"
            raise FujiConfigurationError(msg) from exc
        sys.stdout.write(f"wrote {len(settings)} settings to {args.out}\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-configure",
        description="Work with a Fuji ZP-series analyzer's settings.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser(
        "dump",
        help="Read every setting and write it as JSON. Sends read requests only.",
        description="Read every setting and write it as JSON. Sends read requests only.",
    )
    add_open_args(dump)
    dump.add_argument(
        "--alarm-target",
        action="append",
        type=_parse_target,
        metavar="N=CHn",
        help="Alarm N's target channel (repeatable), to scale its limits, e.g. 1=CH1.",
    )
    dump.add_argument("--out", metavar="FILE", help="Write the JSON document to FILE.")
    dump.add_argument("--format", choices=("text", "json"), default="json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code (0 ok, 1 library error, 2 bad arguments)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    check_open_args(parser, args)
    return run_async_cli(lambda: _dump(args))


if __name__ == "__main__":
    raise SystemExit(main())
