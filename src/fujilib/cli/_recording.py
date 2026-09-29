"""What ``fuji-stream`` and ``fuji-capture`` share: recording arguments, Ctrl-C, reports.

Both record until ``--duration`` has passed, or until Ctrl-C. Ctrl-C is how an
open-ended recording is meant to end, so it is a clean stop: what was
recorded is written and closed, the summary is printed, and the exit code is
0. On Windows, Ctrl-Break stops them the same way; it still works in a
window whose processes ignore Ctrl-C, which a window opened by a process that
ignores it passes on to everything started in it. A connection failure ends a
recording with exit code 1, unless ``--reconnect`` asked for the analyzer to
be reopened instead.
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fujilib.cli._common import run_async_cli
from fujilib.streaming.recorder import OverflowPolicy, ReconnectPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator
    from types import FrameType

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.session import Session
    from fujilib.streaming.recorder import AcquisitionSummary

__all__ = [
    "RecordingState",
    "add_record_args",
    "check_record_args",
    "ctrl_break_as_ctrl_c",
    "device_name",
    "interrupted_note",
    "reconnect_policy",
    "run_recording_cli",
    "summary_report",
]

#: The fastest rate the commands accept; a poll takes about 0.12 s (design §2.4).
_MAX_RATE_HZ: Final = 20.0

#: Windows' Ctrl-Break signal; ``None`` where there is none.
_CTRL_BREAK: Final[int | None] = getattr(signal, "SIGBREAK", None)


def _rate(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= _MAX_RATE_HZ):
        msg = f"expected polls per second, more than 0 and at most {_MAX_RATE_HZ:g}; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _duration(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        msg = f"expected seconds, more than 0; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _buffer(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        msg = f"expected a number of batches, at least 1; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def add_record_args(parser: argparse.ArgumentParser) -> None:
    """Add the arguments that say how to record."""
    parser.add_argument(
        "--rate", type=_rate, default=1.0, metavar="HZ", help="Polls per second (default: 1)."
    )
    parser.add_argument(
        "--duration",
        type=_duration,
        metavar="SECONDS",
        help="How long to record (default: until Ctrl-C).",
    )
    parser.add_argument(
        "--name",
        help="The name the analyzer's rows carry (default: its model, e.g. zpa).",
    )
    parser.add_argument(
        "--overflow",
        choices=tuple(p.value for p in OverflowPolicy),
        default=OverflowPolicy.BLOCK.value,
        help="When the buffer is full: wait, or drop the newest or oldest batch (default: block).",
    )
    parser.add_argument(
        "--buffer-size",
        type=_buffer,
        default=64,
        metavar="N",
        help="Batches waiting to be written before the overflow policy applies (default: 64).",
    )
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help=(
            "After a connection failure, keep recording (error rows) and reopen the port "
            "instead of stopping. Needs a serial port, not --fixture."
        ),
    )


def check_record_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Exit with a usage error on ``--reconnect`` with ``--fixture``, which cannot be reopened."""
    if args.reconnect and args.fixture is not None:
        parser.error("--reconnect needs a serial port; a --fixture cannot be reopened")


def reconnect_policy(args: argparse.Namespace) -> ReconnectPolicy | None:
    """The policy ``--reconnect`` asks for."""
    return ReconnectPolicy() if args.reconnect else None


def device_name(args: argparse.Namespace, analyzer: Analyzer) -> str:
    """``--name``, or the analyzer's model in lower case."""
    if args.name:
        return str(args.name)
    info = analyzer.info
    return info.model.lower() if info is not None else "analyzer"


def summary_report(summary: AcquisitionSummary, session: Session) -> dict[str, object]:
    """The recording's counters, and the session's traffic counters."""
    report: dict[str, object] = {
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat() if summary.finished_at else None,
        "polls": summary.samples_emitted,
        "late": summary.samples_late,
        "dropped": summary.samples_dropped,
        "failed_polls": summary.error_samples,
        "disconnects": summary.disconnects,
        "reconnects": summary.reconnects,
        "max_drift_ms": round(summary.max_drift_ms, 3),
        "target_polls": summary.target_total_samples,
    }
    counters = session.counters
    report["traffic"] = {
        "requests": counters.requests,
        "retries": counters.retries,
        "recovered": session.recoverable_error_count,
        "failures": {kind.value: n for kind, n in sorted(counters.failures.items())},
    }
    return report


@dataclass(slots=True)
class RecordingState:
    """What a recording command knows, for the report after Ctrl-C."""

    summary: AcquisitionSummary | None = None
    session: Session | None = None

    def report(self) -> dict[str, object] | None:
        """The recording's and the session's counters, or ``None`` before it started."""
        if self.summary is None or self.session is None:
            return None
        return summary_report(self.summary, self.session)


@contextmanager
def ctrl_break_as_ctrl_c() -> Generator[None]:
    """While the block runs, Ctrl-Break does whatever Ctrl-C does.

    Entered on the event loop, whose runner has installed its Ctrl-C handler by
    then, so Ctrl-Break cancels the command as Ctrl-C does. Nothing changes
    where there is no Ctrl-Break, or when Ctrl-C has no handler to share.
    """
    on_ctrl_c = signal.getsignal(signal.SIGINT)
    if _CTRL_BREAK is None or not callable(on_ctrl_c):
        yield
        return

    def on_ctrl_break(signum: int, frame: FrameType | None) -> None:
        del signum
        on_ctrl_c(signal.SIGINT, frame)

    previous = signal.signal(_CTRL_BREAK, on_ctrl_break)
    try:
        yield
    finally:
        _ = signal.signal(_CTRL_BREAK, previous)


def run_recording_cli(body: Callable[[], Awaitable[int]], on_interrupt: Callable[[], None]) -> int:
    """Run a recording command; Ctrl-C or Ctrl-Break runs ``on_interrupt`` and exits 0."""

    async def stoppable() -> int:
        with ctrl_break_as_ctrl_c():
            return await body()

    try:
        return run_async_cli(stoppable)
    except KeyboardInterrupt:
        on_interrupt()
        return 0


def interrupted_note() -> None:
    """Say on stderr that the recording was stopped by the user."""
    sys.stderr.write("stopped by Ctrl-C\n")
