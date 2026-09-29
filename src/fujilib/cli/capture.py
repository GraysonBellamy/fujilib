"""``fuji-capture`` — record an analyzer to a CSV or Parquet file.

Read-only: it sends read requests and nothing else. It identifies the
analyzer, reads its metadata (response times, calibration gases, hold mode,
clock), then records at ``--rate`` until ``--duration`` has passed or Ctrl-C.

Two files are written:

- ``--out``: one row per poll, with the columns of
  :func:`~fujilib.sinks.base.sample_to_row`, fixed when the recording starts.
  A failed poll is a row too. The format follows the extension (``.csv``,
  ``.parquet``) unless ``--format`` says otherwise; Parquet needs the
  ``parquet`` extra. CSV is flushed after every write; a Parquet file is
  readable only once it is closed, which Ctrl-C still does.
- ``<out>.meta.json`` (format ``fujilib-capture/1``): what was recorded and
  how: the analyzer's identity and metadata, the arguments, the package
  versions, and, when the recording ends, how it ended and its counters.
  It is written when the recording starts and again when it ends. A Parquet
  file carries the starting version in its metadata as ``fujilib.capture``.

Existing files are not replaced without ``--force``.

Examples::

    fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 --out run.parquet
    fuji-capture COM8 --out run.csv --rate 2 --duration 3600 --reconnect
    fuji-capture --fixture bench --out demo.csv --duration 10
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from fujilib.cli._common import add_open_args, check_open_args, open_from_args, render
from fujilib.cli._recording import (
    RecordingState,
    add_record_args,
    check_record_args,
    device_name,
    interrupted_note,
    reconnect_policy,
    run_recording_cli,
)
from fujilib.cli._report import info_report, metadata_report, ranges_report
from fujilib.errors import FujiConfigurationError
from fujilib.sinks.base import pipe
from fujilib.sinks.csv import CsvSink
from fujilib.sinks.parquet import ParquetSink, require_pyarrow
from fujilib.streaming.poll_source import PollSourceAdapter
from fujilib.streaming.recorder import OverflowPolicy, record

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.models import AnalyzerMetadata
    from fujilib.sinks.base import BaseSink
    from fujilib.streaming.recorder import AcquisitionSummary

__all__ = ["CAPTURE_FORMAT", "main", "metadata_path"]

#: The format tag of the ``.meta.json`` document.
CAPTURE_FORMAT: Final = "fujilib-capture/1"

_SUFFIXES: Final = {".csv": "csv", ".parquet": "parquet", ".pq": "parquet"}
_PROGRESS_S: Final = 10.0


def metadata_path(out: Path) -> Path:
    """The ``.meta.json`` file written beside ``out``."""
    return out.with_name(out.name + ".meta.json")


def _versions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for package in ("fujilib", "anymodbus", "anyserial", "anyio", "pyarrow"):
        try:
            found[package] = version(package)
        except PackageNotFoundError:
            found[package] = None
    found["python"] = platform.python_version()
    found["platform"] = platform.platform()
    return found


def _document(
    args: argparse.Namespace, anz: Analyzer, meta: AnalyzerMetadata, name: str
) -> dict[str, object]:
    info = anz.info
    metadata = metadata_report(meta)
    metadata["clock"] = meta.clock.isoformat() if meta.clock is not None else None
    metadata["clock_read_at"] = meta.clock_read_at.isoformat() if meta.clock_read_at else None
    return {
        "format": CAPTURE_FORMAT,
        "state": "recording",
        "data": {"path": str(args.out), "format": args.resolved_format},
        "device": name,
        "channels": [c.channel.value for c in anz.channels],
        "arguments": {
            "port": args.port,
            "fixture": args.fixture,
            "address": args.address,
            "timeout_s": args.timeout,
            "gas": {c.value: g.value for c, g in args.gas or ()},
            "rate_hz": args.rate,
            "duration_s": args.duration,
            "overflow": args.overflow,
            "buffer_size": args.buffer_size,
            "reconnect": args.reconnect,
        },
        "versions": _versions(),
        "analyzer": {**info_report(info), "ranges": ranges_report(info.ranges)} if info else None,
        "metadata": metadata,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "summary": None,
        "error": None,
    }


async def _write_json(path: Path, document: dict[str, object]) -> None:
    text = json.dumps(document, indent=2, default=str) + "\n"
    try:
        _ = await anyio.Path(path).write_text(text, encoding="utf-8")
    except OSError as exc:
        msg = f"cannot write {path}: {exc}"
        raise FujiConfigurationError(msg) from exc


async def _progress(summary: AcquisitionSummary) -> None:
    started = anyio.current_time()
    while True:
        await anyio.sleep(_PROGRESS_S)
        sys.stderr.write(
            f"{anyio.current_time() - started:6.0f} s: {summary.samples_emitted} polls, "
            f"{summary.error_samples} failed, {summary.samples_late} late\n"
        )


def _sink(args: argparse.Namespace, anz: Analyzer, document: dict[str, object]) -> BaseSink:
    channels = [c.channel for c in anz.channels]
    if args.resolved_format == "parquet":
        start = json.dumps(document, default=str)
        return ParquetSink(args.out, channels=channels, metadata={"fujilib.capture": start})
    return CsvSink(args.out, channels=channels)


async def _capture(args: argparse.Namespace, state: RecordingState) -> int:
    if args.resolved_format == "parquet":
        require_pyarrow()
    out = Path(args.out)
    sidecar = metadata_path(out)
    try:
        await anyio.Path(out.parent).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"cannot create the directory of {out}: {exc}"
        raise FujiConfigurationError(msg) from exc
    async with open_from_args(args) as anz:
        state.session = anz.session
        meta = await anz.read_metadata()
        name = device_name(args, anz)
        document = _document(args, anz, meta, name)
        sink = _sink(args, anz, document)
        await _write_json(sidecar, document)
        ending, error = "failed", None
        try:
            async with (
                sink,
                record(
                    PollSourceAdapter(name, anz),
                    rate_hz=args.rate,
                    duration=args.duration,
                    overflow=OverflowPolicy(args.overflow),
                    buffer_size=args.buffer_size,
                    reconnect=reconnect_policy(args),
                ) as rec,
            ):
                state.summary = rec.summary
                async with anyio.create_task_group() as tg:
                    if not args.quiet:
                        _ = tg.start_soon(_progress, rec.summary)
                    _ = await pipe(rec, sink)
                    tg.cancel_scope.cancel()
            ending = "finished"
        except (anyio.get_cancelled_exc_class(), KeyboardInterrupt):
            ending = "stopped"
            raise
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            document["state"] = ending
            document["finished_at"] = datetime.now(UTC).isoformat()
            document["error"] = error
            document["summary"] = state.report()
            with anyio.CancelScope(shield=True):
                await _write_json(sidecar, document)
    sys.stdout.write(render(_report(out, state), "text"))
    return 0


def _report(out: Path, state: RecordingState) -> dict[str, object]:
    """What the command prints at the end, however the recording ended."""
    return {"data": str(out), "metadata": str(metadata_path(out)), **(state.report() or {})}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-capture",
        description=(
            "Record a Fuji ZP-series analyzer to a CSV or Parquet file, with its "
            "metadata beside it. Sends read requests only."
        ),
    )
    add_open_args(parser)
    add_record_args(parser)
    parser.add_argument("--out", required=True, metavar="FILE", help="The file to write.")
    parser.add_argument(
        "--format",
        choices=("csv", "parquet"),
        help="The file format (default: from the extension of --out).",
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace --out and its .meta.json if they exist."
    )
    parser.add_argument(
        "--quiet", action="store_true", help="No progress line every 10 s on standard error."
    )
    return parser


def _check_out(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    out = Path(args.out)
    resolved = args.format or _SUFFIXES.get(out.suffix.lower())
    if resolved is None:
        parser.error(f"cannot tell the format of {args.out!r} from its extension; give --format")
    args.resolved_format = resolved
    if not args.force:
        for path in (out, metadata_path(out)):
            if path.exists():
                parser.error(f"{path} exists; give --force to replace it")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code (0 ok or Ctrl-C, 1 library error, 2 bad arguments)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    check_open_args(parser, args)
    check_record_args(parser, args)
    _check_out(parser, args)
    state = RecordingState()

    def on_interrupt() -> None:
        interrupted_note()
        if state.summary is not None:
            sys.stdout.write(render(_report(Path(args.out), state), "text"))

    return run_recording_cli(lambda: _capture(args, state), on_interrupt)


if __name__ == "__main__":
    raise SystemExit(main())
