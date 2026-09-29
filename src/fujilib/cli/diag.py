"""``fuji-diag`` — diagnostics. ``fuji-diag timing`` measures the link's timing needs.

Read-only: every request is an FC04 read of one word. ``timing`` runs trials
of two reads, "first" then "second", each either a normal read (input 0000h)
or one the analyzer answers with exception 02 (00C2h, just past the map), in
all four pairings. Before the second read it busy-waits a gap measured from
the moment the first read returned, one of ``--gaps-ms``. Trials run in a
random order (``--seed`` repeats one), with no retries and no inter-frame
idle of the library's own, so a failure shows as a failure. This is the
method of design §2.4 and protocol findings §6.3.

A trial fails when the second read does not get the answer it should: a
word, or exception 02. A trial whose first read did not get its answer is
not judged (``first-bad``): the library then waits out a quiet window inside
the second read, so its gap is not the one measured. The table gives the
failures per pairing and gap; ``--out`` writes every trial, with the
arguments and package versions, as JSON (format ``fujilib-diag-timing/1``),
also what was measured before a failure or Ctrl-C. An existing ``--out`` is
replaced only with ``--force``.

Examples::

    fuji-diag timing COM8
    fuji-diag timing COM8 --trials 250 --gaps-ms 0,1,2,5 --out timing.json
    fuji-diag timing --fixture bench --trials 5
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from fujilib.cli._common import BENCH_FIXTURE, seconds, station
from fujilib.cli._recording import run_recording_cli
from fujilib.devices.profile import ZP_PROFILE
from fujilib.errors import (
    FujiConfigurationError,
    FujiConnectionError,
    FujiError,
    FujiFrameError,
    FujiModbusError,
    FujiModbusIllegalDataAddressError,
    FujiModbusTimeoutError,
    FujiValidationError,
)
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.protocol.modbus.read_plan import BlockRead
from fujilib.registry.regions import FC_READ_INPUT
from fujilib.testing import BENCH_BANK_PATH, MockAnalyzer, load_bank, mock_transport
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from fujilib.protocol.modbus.client import ModbusClient

__all__ = ["TIMING_FORMAT", "main"]

#: The format tag of ``fuji-diag timing --out``.
TIMING_FORMAT: Final = "fujilib-diag-timing/1"

#: The two kinds of read: a word, or exception 02 just past the input map (design §2.3).
_READS: Final = {
    "normal": BlockRead(FC_READ_INPUT, 0x0000, 1),
    "exception": BlockRead(FC_READ_INPUT, 0x00C2, 1),
}
_EXPECTED: Final = {"normal": "ok", "exception": "exception 02"}
#: Idle before each trial's first read, so trials do not affect each other.
_TRIAL_SETTLE_S: Final = 0.010
_MS: Final = 1000.0
_MAX_GAP_MS: Final = 1000.0
_MAX_TRIALS: Final = 10_000


def _outcome(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, FujiModbusIllegalDataAddressError):
        return "exception 02"
    if isinstance(exc, FujiModbusTimeoutError):
        return "timeout"
    if isinstance(exc, FujiModbusError):
        code = exc.context.extra.get("exception_code")
        return f"exception {code:02d}" if isinstance(code, int) else "exception"
    if isinstance(exc, FujiFrameError):
        return "damaged"
    return type(exc).__name__


async def _read(client: ModbusClient, kind: str) -> tuple[str, float | None]:
    """One read: its outcome and, for a word, its round trip in ms."""
    try:
        reply = await client.read(_READS[kind], command="diag.timing")
    except FujiConnectionError:
        raise
    except FujiError as exc:
        return _outcome(exc), None
    return _outcome(None), reply.timing.latency_s * _MS


def _spin_until(deadline_ns: int) -> None:
    """Busy-wait: a sleep this short is rounded up to about 16 ms on Windows (design §2.4)."""
    while time.monotonic_ns() < deadline_ns:
        pass


async def _trial(client: ModbusClient, first: str, second: str, gap_ms: float) -> dict[str, object]:
    await anyio.sleep(_TRIAL_SETTLE_S)
    first_outcome, _ = await _read(client, first)
    returned = time.monotonic_ns()
    _spin_until(returned + round(gap_ms * 1e6))
    sent = time.monotonic_ns()
    second_outcome, round_trip = await _read(client, second)
    # A trial counts only when its first read got the answer it should: after a
    # damaged or lost first reply the library waits out a quiet window inside
    # the second read's call, so the gap measured is not the gap on the wire.
    judged = first_outcome == _EXPECTED[first]
    return {
        "first": first,
        "second": second,
        "target_gap_ms": gap_ms,
        "gap_ms": round((sent - returned) / 1e6, 3),
        "first_outcome": first_outcome,
        "second_outcome": second_outcome,
        "second_round_trip_ms": None if round_trip is None else round(round_trip, 3),
        "judged": judged,
        "failed": judged and second_outcome != _EXPECTED[second],
    }


def summarize(trials: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Per pairing and gap: trials, failures, first reads that misbehaved, gaps.

    A trial whose first read misbehaved is counted in ``first_bad`` and not
    judged: its second read is neither a success nor a failure.
    """
    cells: dict[tuple[str, str, float], list[dict[str, object]]] = {}
    for trial in trials:
        key = (str(trial["first"]), str(trial["second"]), float(str(trial["target_gap_ms"])))
        cells.setdefault(key, []).append(trial)
    rows: list[dict[str, object]] = []
    for (first, second, gap), group in sorted(cells.items()):
        gaps = sorted(float(str(t["gap_ms"])) for t in group)
        failures: dict[str, int] = {}
        for t in group:
            if t["failed"]:
                outcome = str(t["second_outcome"])
                failures[outcome] = failures.get(outcome, 0) + 1
        rows.append(
            {
                "first": first,
                "second": second,
                "target_gap_ms": gap,
                "trials": len(group),
                "failed": sum(failures.values()),
                "first_bad": sum(t["first_outcome"] != _EXPECTED[first] for t in group),
                "gap_ms": {
                    "min": gaps[0],
                    "median": statistics.median(gaps),
                    "max": gaps[-1],
                },
                "failures": failures,
            }
        )
    return rows


def _table(rows: Sequence[dict[str, object]]) -> str:
    lines = [
        "first      second     gap ms  trials  failed  first-bad  measured gap ms (min/median/max)"
    ]
    for row in rows:
        gaps = row["gap_ms"]
        assert isinstance(gaps, dict)  # noqa: S101 - built by summarize
        lines.append(
            f"{row['first']!s:<10} {row['second']!s:<10} {row['target_gap_ms']!s:>6}  "
            f"{row['trials']!s:>6}  {row['failed']!s:>6}  {row['first_bad']!s:>9}  "
            f"{gaps['min']:.2f} / {gaps['median']:.2f} / {gaps['max']:.2f}"
            + (f"  {row['failures']}" if row["failures"] else "")
        )
    return "\n".join(lines) + "\n"


@asynccontextmanager
async def _port(args: argparse.Namespace) -> AsyncGenerator[ModbusPort]:
    """A port with no idle of its own and no retries, on the analyzer the arguments name."""
    async with AsyncExitStack() as stack:
        if args.fixture is None:
            settings = replace(ZP_PROFILE.default_serial, port=args.port)
            transport = await SerialTransport.open(settings)
            _ = stack.push_async_callback(transport.aclose)
        else:
            path = BENCH_BANK_PATH if args.fixture == BENCH_FIXTURE else Path(args.fixture)
            try:
                bank = load_bank(path, station=args.address)
            except (OSError, ValueError, TypeError) as exc:
                msg = f"cannot read the register bank {str(path)!r}: {exc}"
                raise FujiValidationError(msg) from exc
            simulated = mock_transport(MockAnalyzer(bank), label=f"fixture:{path.name}")
            transport, _line = await stack.enter_async_context(simulated)
        port = ModbusPort(
            transport, request_timeout=args.timeout, inter_frame_idle=0.0, read_retries=0
        )
        yield await stack.enter_async_context(port)


async def _timing(args: argparse.Namespace, trials: list[dict[str, object]]) -> int:
    started = datetime.now(UTC)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    pairings = [(a, b) for a in _READS for b in _READS]
    plan = [(a, b, g) for a, b in pairings for g in args.gaps_ms for _ in range(args.trials)]
    random.Random(seed).shuffle(plan)  # noqa: S311 - a reproducible order, not security
    if args.out is not None:
        try:
            await anyio.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"cannot create the directory of {args.out}: {exc}"
            raise FujiConfigurationError(msg) from exc
    ending: dict[str, object] = {"completed": False, "error": None}
    try:
        async with _port(args) as port:
            client = port.client(args.address)
            _ = await _read(client, "normal")  # the adapter's first read is not counted
            for first, second, gap in plan:
                trials.append(await _trial(client, first, second, gap))
        ending["completed"] = True
    except (anyio.get_cancelled_exc_class(), KeyboardInterrupt):
        raise
    except BaseException as exc:
        ending["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # Whatever was measured is kept, also when the run is stopped or fails.
        with anyio.CancelScope(shield=True):
            await _report(args, trials, seed, started, ending)
    return 0


async def _report(
    args: argparse.Namespace,
    trials: list[dict[str, object]],
    seed: int,
    started: datetime,
    ending: dict[str, object],
) -> None:
    rows = summarize(trials)
    sys.stdout.write(_table(rows))
    if args.out is not None:
        document = {
            "format": TIMING_FORMAT,
            **ending,
            "arguments": {
                "port": args.port,
                "fixture": args.fixture,
                "address": args.address,
                "request_timeout_s": args.timeout,
                "gaps_ms": args.gaps_ms,
                "trials_per_cell": args.trials,
                "seed": seed,
            },
            "method": {
                "reads": {
                    k: f"FC04 {r.address:04X}h x1 -> {_EXPECTED[k]}" for k, r in _READS.items()
                },
                "retries": 0,
                "library_idle_ms": 0.0,
                "trial_settle": "a sleep of at least 10 ms (about 16 ms on Windows)",
                "gap": "busy-wait from the first read's return to the second read's call",
                "judged": "a trial counts only when its first read got the answer it should",
            },
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "versions": {
                **{p: version(p) for p in ("fujilib", "anymodbus", "anyserial", "anyio")},
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "summary": rows,
            "trials": trials,
        }
        try:
            _ = await anyio.Path(args.out).write_text(
                json.dumps(document, indent=1) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            msg = f"cannot write {args.out}: {exc}"
            raise FujiConfigurationError(msg) from exc
        sys.stdout.write(f"{len(trials)} trials written to {args.out}\n")


def _gaps(text: str) -> list[float]:
    try:
        gaps = [float(g) for g in text.split(",")]
    except ValueError:
        gaps = []
    if not gaps or not all(0 <= g <= _MAX_GAP_MS for g in gaps):
        msg = f"expected gaps in ms, 0-1000, separated by commas; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return gaps


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= _MAX_TRIALS:
        msg = f"expected trials per cell, 1-10000; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-diag", description="Diagnostics for a Fuji ZP-series analyzer."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    timing = commands.add_parser(
        "timing",
        help="Measure the gap the analyzer needs between requests. Sends reads only.",
        description=(
            "Measure the gap the analyzer needs between requests: pairs of one-word reads,"
            " normal or answered with exception 02, with busy-waited gaps in a random order"
            " and no retries. Sends reads only."
        ),
    )
    timing.add_argument("port", nargs="?", help="Serial port, e.g. COM8 or /dev/ttyUSB0.")
    timing.add_argument(
        "--fixture",
        metavar="BANK",
        help=f"Answer from a register bank on a simulated analyzer; {BENCH_FIXTURE!r} is bundled.",
    )
    timing.add_argument("--address", type=station, default=1, help="Station number (default: 1).")
    timing.add_argument(
        "--timeout",
        type=seconds,
        default=0.3,
        help="Seconds to wait for each reply (default: 0.3).",
    )
    timing.add_argument(
        "--gaps-ms",
        type=_gaps,
        default=[0.0, 1.0, 2.0, 5.0],
        metavar="LIST",
        help="The gaps to test, in ms (default: 0,1,2,5).",
    )
    timing.add_argument(
        "--trials",
        type=_count,
        default=50,
        metavar="N",
        help="Trials per pairing and gap (default: 50; the findings used 250).",
    )
    timing.add_argument("--seed", type=int, help="Repeat a trial order.")
    timing.add_argument("--out", metavar="FILE", help="Write every trial as JSON.")
    timing.add_argument("--force", action="store_true", help="Replace --out if it exists.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code (0 ok, 1 library error, 2 bad arguments)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if (args.port is None) == (args.fixture is None):
        parser.error("give either a serial port or --fixture")
    if args.out is not None and not args.force and Path(args.out).exists():
        parser.error(f"{args.out} exists; give --force to replace it")
    trials: list[dict[str, object]] = []

    def on_interrupt() -> None:
        sys.stderr.write(f"stopped by Ctrl-C after {len(trials)} trials\n")

    return run_recording_cli(lambda: _timing(args, trials), on_interrupt)


if __name__ == "__main__":
    raise SystemExit(main())
