"""``fuji-stream`` — poll an analyzer at a fixed rate and print each poll as it comes.

Read-only: it sends read requests and nothing else. One line per poll on
standard output: readable text (the default), CSV with a header, or JSON
lines, each with the columns of :func:`~fujilib.sinks.base.sample_to_row`.
A failed poll is a line too. It runs until ``--duration`` has passed or
Ctrl-C, then prints the recording's counters on standard error.

Examples::

    fuji-stream COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2
    fuji-stream COM8 --rate 2 --duration 60 --format csv > run.csv
    fuji-stream --fixture bench --duration 5 --format jsonl
"""

from __future__ import annotations

import argparse
import csv
import errno
import io
import json
import os
import sys
from typing import TYPE_CHECKING

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
from fujilib.sinks.base import row_columns, sample_to_row
from fujilib.sinks.csv import csv_cell
from fujilib.streaming.poll_source import PollSourceAdapter
from fujilib.streaming.recorder import OverflowPolicy, record

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib.streaming.sample import Sample

__all__ = ["main", "text_line"]


def text_line(sample: Sample) -> str:
    """One poll as a line of text: time, name, each channel, then any analyzer error or alarm."""
    head = f"{sample.t_utc.isoformat(timespec='milliseconds')}  {sample.device}"
    if sample.error is not None:
        return f"{head}  failed: {type(sample.error).__name__}: {sample.error}"
    frame = sample.frame
    assert frame is not None  # noqa: S101 - a sample has a frame or an error
    readings = {r.channel: r for r in frame.readings}
    parts: list[str] = []
    for channel in sample.channels:
        reading = readings.get(channel)
        if reading is None:
            parts.append(f"{channel.value} -")
            continue
        value = "-" if reading.value is None else f"{reading.as_decimal()}"
        label = f"{channel.value} {reading.gas.value}"
        parts.append(f"{label} {value} {reading.unit.value} {reading.state.value}")
    line = f"{head}  " + "  ".join(parts)
    status = frame.analyzer
    if status is not None:
        flags = [f"error {int(e)}" for e in sorted(status.errors)]
        flags += [f"alarm {n}" for n, a in enumerate(status.alarms, start=1) if int(a) != 0]
        if status.auto_calibration_running:
            flags.append("auto calibration")
        if flags:
            line += "  [" + ", ".join(flags) + "]"
    return line


class _ReaderGone(Exception):  # noqa: N818 - not an error: the reader stopped reading
    """Standard output's reader went away, e.g. ``fuji-stream ... | head``."""


def _closed_pipe(exc: OSError) -> bool:
    """Whether ``exc`` is a write to a pipe nobody reads: EPIPE, or EINVAL on Windows."""
    return isinstance(exc, BrokenPipeError) or exc.errno in {errno.EPIPE, errno.EINVAL}


def _silence_stdout() -> None:
    """Point standard output at the null device, so the exit flush cannot fail again."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, io.UnsupportedOperation):
        pass  # not a real file (e.g. under a test's capture)
    finally:
        os.close(devnull)


async def _stream(args: argparse.Namespace, state: RecordingState) -> int:
    try:
        return await _print_polls(args, state)
    except _ReaderGone:
        _silence_stdout()
        return 0


async def _print_polls(args: argparse.Namespace, state: RecordingState) -> int:
    out = sys.stdout

    def emit(text: str) -> None:
        try:
            out.write(text)
            out.flush()
        except OSError as exc:
            if _closed_pipe(exc):
                raise _ReaderGone from exc
            raise

    async with open_from_args(args) as anz:
        state.session = anz.session
        source = PollSourceAdapter(device_name(args, anz), anz)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_STRINGS)
        async with record(
            source,
            rate_hz=args.rate,
            duration=args.duration,
            overflow=OverflowPolicy(args.overflow),
            buffer_size=args.buffer_size,
            reconnect=reconnect_policy(args),
        ) as rec:
            state.summary = rec.summary
            first = True
            async for batch in rec:
                for sample in batch.values():
                    if args.format == "text":
                        buffer.write(text_line(sample) + "\n")
                    elif args.format == "jsonl":
                        buffer.write(json.dumps(sample_to_row(sample)) + "\n")
                    else:
                        columns = [c.name for c in row_columns(sample.channels)]
                        if first:
                            writer.writerow(columns)
                        row = sample_to_row(sample)
                        writer.writerow([csv_cell(row[name]) for name in columns])
                    first = False
                emit(buffer.getvalue())
                _ = buffer.seek(0)
                _ = buffer.truncate()
    sys.stderr.write(render(state.report() or {}, "text"))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-stream",
        description=(
            "Poll a Fuji ZP-series analyzer at a fixed rate and print each poll. "
            "Sends read requests only."
        ),
    )
    add_open_args(parser)
    add_record_args(parser)
    parser.add_argument(
        "--format",
        choices=("text", "csv", "jsonl"),
        default="text",
        help="How each poll is printed (default: text).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code (0 ok or Ctrl-C, 1 library error, 2 bad arguments)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    check_open_args(parser, args)
    check_record_args(parser, args)
    state = RecordingState()

    def on_interrupt() -> None:
        interrupted_note()
        report = state.report()
        if report is not None:
            sys.stderr.write(render(report, "text"))

    return run_recording_cli(lambda: _stream(args, state), on_interrupt)


if __name__ == "__main__":
    raise SystemExit(main())
