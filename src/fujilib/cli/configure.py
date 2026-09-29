"""``fuji-configure`` — the analyzer's settings as a document.

``dump`` reads every holding register and writes them as JSON, keyed by
register name (as in ``docs/registers.md``), never by address. Each entry
has the decoded ``value``, the ``raw`` register value, its ``unit``, and
the register's ``access``, ``safety`` tier and ``evidence``. The document
carries a ``format`` version and the analyzer's identity, so it can be
compared with another analyzer's or a later dump.

``diff`` compares a document with the analyzer and says, for each of its
settings, whether it is unchanged, would be written, or is refused and why.
It only reads.

``apply`` writes the settings of a document that differ, after the same
comparison: if anything is refused, nothing is written. It needs
``--confirm``, and ``--i-understand-this-is-destructive`` as well when a
write is DANGEROUS (the calibration gases and scope). Each write is read
back, and the first that fails stops the rest. ``--dry-run`` stops after the
comparison. It ends with a ``status:`` line (``ok``, ``dry_run``,
``refused``, ``partial``, ``verify_failed``, ``unknown`` or ``failed``) and
exits 1 on anything but ``ok`` or ``dry_run``.

Alarm limits are scaled only when ``--alarm-target`` names each alarm's
channel, because the target register's encoding is contested (design §5.2).

Examples::

    fuji-configure dump COM8 --out zpa-settings.json
    fuji-configure dump --fixture bench --format text
    fuji-configure diff COM8 --file zpa-settings.json
    fuji-configure apply COM8 --file changes.json --confirm
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from fujilib.cli._common import (
    add_open_args,
    check_open_args,
    open_from_args,
    render,
    run_async_cli,
)
from fujilib.cli._report import apply_report, diff_report, settings_report
from fujilib.devices.capability import SafetyTier
from fujilib.devices.settings import SETTINGS_FORMAT, ApplyStatus, SettingsDocument
from fujilib.errors import FujiConfigurationError, FujiError
from fujilib.registry.channels import ChannelId, coerce_channel
from fujilib.version import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib.devices.settings import ApplyReport, SettingsDiff

__all__ = ["DESTRUCTIVE_FLAG", "SETTINGS_FORMAT", "main"]

#: Alarms 1-6, whose target channels ``--alarm-target`` names (design §5.2).
_ALARMS: Final = range(1, 7)

#: The acknowledgement a DANGEROUS write needs, as in the siblings' tools.
DESTRUCTIVE_FLAG: Final = "--i-understand-this-is-destructive"

#: What to do after an apply that did not end ``ok``.
_RECOVERY: Final = {
    ApplyStatus.VERIFY_FAILED: (
        "the setting reads back otherwise, or a range it selects is not in effect: check "
        "the front panel, then compare again with diff"
    ),
    ApplyStatus.UNKNOWN: (
        "the last write may or may not have been applied: run diff before applying again"
    ),
    ApplyStatus.PARTIAL: (
        "fix the cause and apply the document again; settings already written compare as unchanged"
    ),
    ApplyStatus.FAILED: "nothing was changed; fix the cause and apply the document again",
}


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


async def _diff(args: argparse.Namespace, document: SettingsDocument) -> int:
    async with open_from_args(args) as anz:
        diff = await anz.diff_settings(document, any_analyzer=args.any_analyzer)
    sys.stdout.write(render(diff_report(diff), args.format))
    return 0


async def _apply(args: argparse.Namespace, document: SettingsDocument) -> int:
    async with open_from_args(args) as anz:
        diff = await anz.diff_settings(document, any_analyzer=args.any_analyzer)
        if not diff.ok or args.dry_run or not diff.writes:
            return _stop_before_writing(args, diff)
        if diff.tier is SafetyTier.DANGEROUS and not args.destructive:
            sys.stdout.write(render(diff_report(diff), args.format))
            sys.stderr.write(
                f"error: fuji-configure apply would write DANGEROUS settings; pass "
                f"{DESTRUCTIVE_FLAG} to go ahead\n"
            )
            return 2
        # The ceiling is checked again on apply's own comparison, in case the
        # analyzer changed since this one.
        ceiling = SafetyTier.DANGEROUS if args.destructive else SafetyTier.PERSISTENT
        report = await anz.apply_settings(
            document, confirm=True, any_analyzer=args.any_analyzer, max_tier=ceiling
        )
    return _finish(args, report)


def _stop_before_writing(args: argparse.Namespace, diff: SettingsDiff) -> int:
    out = diff_report(diff)
    if not diff.ok:
        out["status"] = "refused"
        out["recovery"] = "nothing was written; remove or correct the refused settings"
    elif args.dry_run and diff.writes:
        out["status"] = "dry_run"
    else:
        out["written"] = {}
        out["status"] = "ok"
    sys.stdout.write(render(out, args.format))
    return 0 if diff.ok else 1


def _finish(args: argparse.Namespace, report: ApplyReport) -> int:
    out = apply_report(report)
    recovery = _RECOVERY.get(report.status)
    if recovery is not None:
        out["recovery"] = recovery
    sys.stdout.write(render(out, args.format))
    return 0 if report.status is ApplyStatus.OK else 1


def _read_document(parser: argparse.ArgumentParser, path: str) -> SettingsDocument:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return SettingsDocument.from_json(data)
    except (OSError, ValueError, FujiError) as exc:
        parser.error(f"cannot read the settings document {path!r}: {exc}")


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

    diff = commands.add_parser(
        "diff",
        help="Compare a settings document with the analyzer. Sends read requests only.",
        description="Compare a settings document with the analyzer. Sends read requests only.",
    )
    add_open_args(diff)
    _add_document_args(diff)

    apply = commands.add_parser(
        "apply",
        help="Write the settings of a document that differ. Changes the analyzer.",
        description=(
            "Write the settings of a document that differ from the analyzer's, each read "
            "back; nothing is written if any setting is refused. Changes the analyzer."
        ),
    )
    add_open_args(apply)
    _add_document_args(apply)
    apply.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge that this changes the analyzer's settings.",
    )
    apply.add_argument(
        DESTRUCTIVE_FLAG,
        dest="destructive",
        action="store_true",
        help="Also required when a write is DANGEROUS: the calibration gases and scope.",
    )
    apply.add_argument(
        "--dry-run", action="store_true", help="Compare, show what would be written, and stop."
    )
    return parser


def _add_document_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--file",
        required=True,
        metavar="FILE",
        help="The settings document (fujilib-settings/1), as dump writes it.",
    )
    parser.add_argument(
        "--any-analyzer",
        action="store_true",
        help="Accept a document written from another analyzer (another serial number).",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code.

    0 ok; 1 a library error, a refused document or an apply that did not end
    ``ok``; 2 bad arguments, or a DANGEROUS apply without its flag.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    check_open_args(parser, args)
    if args.command == "dump":
        return run_async_cli(lambda: _dump(args))
    if args.command == "apply" and not (args.confirm or args.dry_run):
        parser.error("fuji-configure apply changes the analyzer; pass --confirm to go ahead")
    document = _read_document(parser, args.file)
    if args.command == "diff":
        return run_async_cli(lambda: _diff(args, document))
    return run_async_cli(lambda: _apply(args, document))


if __name__ == "__main__":
    raise SystemExit(main())
