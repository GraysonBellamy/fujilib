"""``fuji-calibrate`` — a manual zero or span from the host, with the operator at the gases.

It presses the analyzer's front-panel keys over Modbus (design §6.5): ZERO or
SPAN, the cursor to the channel, ENT to select it. The analyzer then waits for
the gas, and the operator switches the valves to the gas they named. fujilib
watches the reading until it is steady on that gas, asks before the key that
calibrates (unless ``--auto``), sends it, and follows the calibration to its
end. However it ends, Ctrl-C included, the panel is returned to measurement.

Before any key it checks the panel (the measurement screen, no calibration
flag set), that key lock and output hold are off, that no instrument error is
active, that the calibration touches no channel on both ranges, and that the
gas named is the channel's calibration-gas setting. It needs ``--confirm``
for the keys and ``--i-understand-this-is-destructive`` for the calibration.
``--plan`` only says what a zero or span of the channel would calibrate.

Each run is written to a ``fujilib-calibration/1`` document (``--out``, by
default ``fuji-calibration_<serial>_<channel>_<kind>_<time>.json``): the
plan, the gas named, the readings on the wait step and their steadiness, the
keys, the readings before and after, the deviation from the gas and, unless
``--no-adc``, the detectors' raw counts. It ends with a ``status:`` line
(``completed``, ``failed``, ``ambiguous``, ``cancelled``, ``refused``,
``stopped``, ``not_clean`` or ``plan``) and exits 0 on ``completed`` or
``plan``, 1 otherwise, and 2 on bad arguments. While it asks, it goes on
reading the panel, so the gas is judged on reads right up to the key.

Examples::

    fuji-calibrate COM8 --channel CH3 --kind zero --plan
    fuji-calibrate COM8 --gas CH3=o2 --channel CH3 --kind zero --gas-value 0
        --gas-label "N2, cylinder 1234" --confirm --i-understand-this-is-destructive
    fuji-calibrate COM8 --gas CH3=o2 --channel CH3 --kind span --gas-value 20.95
        --gas-unit vol% --confirm --i-understand-this-is-destructive

Each command is one line; it is wrapped here to fit.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import sys
import threading
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from fujilib.cli._common import add_open_args, check_open_args, open_from_args, run_async_cli
from fujilib.cli._recording import ctrl_break_as_ctrl_c
from fujilib.devices.capability import Availability, Capability
from fujilib.devices.keys import (
    CalibrationGas,
    RemoteCalibration,
    RunState,
    calibration_filename,
)
from fujilib.devices.panel import ManualCalibrationKind, ManualCalibrationOutcome
from fujilib.devices.steadiness import SteadinessRule
from fujilib.errors import FujiAnalyzerStateError, FujiError
from fujilib.registry.channels import coerce_channel

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.keys import RemoteCalibrationResult
    from fujilib.devices.panel import ManualCalibrationEvent, ManualCalibrationPlan
    from fujilib.devices.steadiness import SteadinessVerdict
    from fujilib.testing import MockAnalyzer, MockRequest

__all__ = ["DESTRUCTIVE_FLAG", "main"]

#: The acknowledgement the key that calibrates needs, as in the siblings' tools.
DESTRUCTIVE_FLAG: Final = "--i-understand-this-is-destructive"

#: Seconds between progress lines while the reading is unchanged in kind.
_PROGRESS_EVERY_S: Final = 5.0

#: How fast the named gas reaches the simulated analyzer's inlet with ``--fixture``.
_FIXTURE_TAU_S: Final = 0.5
#: The manual-calibration wait steps (30182), zero and span.
_ZERO_WAIT: Final = 5
_WAIT_STEPS: Final = frozenset({_ZERO_WAIT, 8})


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        msg = f"expected a positive number; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _not_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value >= 0):
        msg = f"expected a number of 0 or more; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        msg = f"expected a number; got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-calibrate",
        description=(
            "Make a manual zero or span from the host: fujilib presses the calibration keys, "
            "the operator switches the gas valves."
        ),
    )
    add_open_args(parser)
    parser.add_argument("--channel", required=True, help="The channel to calibrate, e.g. CH3.")
    parser.add_argument("--kind", required=True, choices=[k.value for k in ManualCalibrationKind])
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Only say what the calibration would touch; read-only, no key.",
    )
    gas = parser.add_argument_group("the gas the operator puts at the inlet")
    gas.add_argument(
        "--gas-value",
        type=_number,
        help="Its concentration, which must be the channel's calibration-gas setting.",
    )
    gas.add_argument(
        "--gas-unit", help="Its unit, the channel's (vol%%, ppm, ...); optional for a gas of 0."
    )
    gas.add_argument("--gas-label", help="What it is, kept in the record, e.g. 'N2, lot 1234'.")
    keys = parser.add_argument_group("going ahead")
    keys.add_argument("--confirm", action="store_true", help="Press the analyzer's keys.")
    keys.add_argument(
        DESTRUCTIVE_FLAG,
        dest="destructive",
        action="store_true",
        help="Send the key that calibrates, which changes the analyzer's calibration.",
    )
    keys.add_argument(
        "--auto",
        action="store_true",
        help="Calibrate once the gas is steady, without asking.",
    )
    rule = parser.add_argument_group("when the gas is steady (design §13.1 #78)")
    defaults = SteadinessRule()
    rule.add_argument(
        "--window",
        type=_positive,
        default=defaults.window_s,
        help=f"Shortest window, in s (default: {defaults.window_s:g}).",
    )
    rule.add_argument(
        "--response-factor",
        type=_not_negative,
        default=defaults.response_factor,
        help=(
            "The window is at least this many response times "
            f"(default: {defaults.response_factor:g})."
        ),
    )
    rule.add_argument(
        "--band",
        type=_positive,
        default=defaults.band_percent_fs,
        help=f"How far the reading may move over the window, in %%FS "
        f"(default: {defaults.band_percent_fs:g}).",
    )
    rule.add_argument(
        "--tolerance",
        type=_positive,
        default=defaults.tolerance_percent_fs,
        help=f"How far from the named gas it may read, in %%FS "
        f"(default: {defaults.tolerance_percent_fs:g}).",
    )
    rule.add_argument(
        "--settle-timeout",
        type=_positive,
        default=defaults.timeout_s,
        help=f"Give up after this many s (default: {defaults.timeout_s:g}).",
    )
    rule.add_argument(
        "--max-gap",
        type=_positive,
        default=defaults.max_gap_s,
        help=f"The longest time between two reads in the window, in s "
        f"(default: {defaults.max_gap_s:g}).",
    )
    rule.add_argument(
        "--interval",
        type=_positive,
        default=0.5,
        help="Seconds between reads on the wait step (default: 0.5).",
    )
    record = parser.add_argument_group("the record")
    record.add_argument("--out", type=Path, help="Where to write the record (JSON).")
    record.add_argument("--force", action="store_true", help="Replace an existing --out.")
    record.add_argument(
        "--no-adc", action="store_true", help="Do not read the detectors' raw counts."
    )
    record.add_argument("--operator", help="Who made the calibration, kept in the record.")
    record.add_argument("--notes", help="Anything else to keep in the record.")
    return parser


def _check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> CalibrationGas | None:
    """Refuse bad arguments before the port is opened; the named gas."""
    check_open_args(parser, args)
    try:
        args.channel = coerce_channel(args.channel)
    except FujiError as exc:
        parser.error(str(exc))
    if not args.channel.is_measured:
        parser.error(f"{args.channel.value} is not a measured channel; only CH1-CH5 are calibrated")
    if args.plan:
        return None
    if args.gas_value is None:
        parser.error("name the gas at the inlet with --gas-value (and --gas-unit)")
    if not args.confirm:
        parser.error("fuji-calibrate presses the analyzer's keys; pass --confirm to go ahead")
    if not args.destructive:
        parser.error(f"a calibration changes the analyzer's calibration; pass {DESTRUCTIVE_FLAG}")
    if args.interval >= args.max_gap:
        parser.error("--interval must be shorter than --max-gap, or the gas is never steady")
    if args.out is not None and args.out.exists() and not args.force:
        parser.error(f"{args.out} exists; pass --force to replace it")
    if args.out is not None and not args.out.parent.is_dir():
        parser.error(f"{args.out.parent} is not a directory; the record cannot be written there")
    try:
        return CalibrationGas(args.gas_value, args.gas_unit, args.gas_label)
    except FujiError as exc:
        parser.error(str(exc))


def _say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _describe_plan(plan: ManualCalibrationPlan) -> list[str]:
    lines = [f"A manual {plan.kind.value} of {plan.channel.value} calibrates:"]
    for target in plan.targets:
        gases = target.zero_gas if plan.kind is ManualCalibrationKind.ZERO else target.span_gas
        ranges = ", ".join(
            f"range {r} against {'?' if g is None else f'{g:g}'} {u}"
            for r, g, u in zip(target.ranges, gases, target.units, strict=True)
        )
        fitted = "" if target.established else " (not established: may not be fitted)"
        lines.append(f"  {target.channel.value}: {ranges}{fitted}")
    lines.extend(f"  note: {note}" for note in plan.notes)
    return lines


class _Progress:
    """Prints the steadiness verdicts: when their reasons change, and every few seconds."""

    def __init__(self) -> None:
        self._last: tuple[str, ...] | None = None
        self._at = -math.inf

    def __call__(self, verdict: SteadinessVerdict) -> None:
        kinds = tuple(s.reason.split(" ")[0] for s in verdict.channels.values())
        now = verdict.elapsed_s
        if kinds == self._last and now - self._at < _PROGRESS_EVERY_S:
            return
        self._last, self._at = kinds, now
        parts: list[str] = []
        for channel, state in verdict.channels.items():
            shown = "-" if state.last is None else f"{state.last:g}"
            parts.append(f"{channel.value} {shown} {state.unit}: {state.reason}")
        _say(f"  +{now:5.1f} s  " + "; ".join(parts))


async def _ask(question: str) -> bool:
    """Whether the operator answers yes; no answer at all (input closed) is no.

    The prompt waits in a daemon thread, so a Ctrl-C meanwhile leaves nothing
    to hold up the exit.
    """
    answer: list[str | None] = []
    answered = anyio.Event()
    token = anyio.lowlevel.current_token()

    def prompt() -> None:
        try:
            answer.append(input(question))
        except EOFError:
            answer.append(None)
        finally:
            with contextlib.suppress(Exception):  # the loop may have ended meanwhile
                anyio.from_thread.run_sync(answered.set, token=token)

    threading.Thread(target=prompt, name="fuji-calibrate prompt", daemon=True).start()
    await answered.wait()
    if answer[0] is None:
        _say("")
        return False
    return answer[0].strip().lower() in {"y", "yes"}


async def _ask_reading(run: RemoteCalibration, question: str, interval: float) -> bool:
    """:func:`_ask`, reading the panel every ``interval`` meanwhile.

    A calibration is judged on the reads just before its key; reading while
    the operator decides keeps them close together.

    Raises:
        FujiError: a read failed, or the panel left the wait step.
    """
    failure: list[FujiError] = []
    yes = False
    async with anyio.create_task_group() as tg:

        async def keep_reading() -> None:
            try:
                while True:
                    await anyio.sleep(interval)
                    await run.read()
            except FujiError as exc:
                failure.append(exc)
                tg.cancel_scope.cancel()

        _ = tg.start_soon(keep_reading)
        yes = await _ask(question)
        tg.cancel_scope.cancel()
    if failure:
        raise failure[0]
    return yes


def _fixture(gas: CalibrationGas | None) -> Callable[[MockAnalyzer], None]:
    """Set up the simulated analyzer of ``--fixture``, with an operator at its gas valves."""

    def setup(mock: MockAnalyzer) -> None:
        # The bench analyzer's panel offers the channels it has; a calibration takes a second.
        mock.config = replace(mock.config, panel_channels=(1, 2, 3), manual_calibration_s=1.0)
        flowing: list[bool] = []

        def operator(request: MockRequest) -> None:
            # Once the wait step opens, the named gas flows to the channels being calibrated.
            step = mock.register("display.calibration_step")[0]
            if gas is None or flowing or step not in _WAIT_STEPS:
                return
            flowing.append(True)
            flag = "zero_calibrating" if step == _ZERO_WAIT else "span_calibrating"
            for n in range(1, 6):
                if mock.register(f"status.ch{n}.{flag}")[0]:
                    mock.flow(f"CH{n}", gas.value, tau_s=_FIXTURE_TAU_S, at=request.arrived_at)

        mock.on_request = operator

    return setup


@dataclass(slots=True)
class _Outcome:
    """What a run left, for the report after a Ctrl-C."""

    result: RemoteCalibrationResult | None = None


async def _calibrate(
    args: argparse.Namespace, gas: CalibrationGas | None, outcome: _Outcome
) -> int:
    async with open_from_args(args, simulated=_fixture(gas)) as anz:
        plan = await anz.plan_manual_calibration(args.channel, args.kind)
        for line in _describe_plan(plan):
            _say(line)
        if gas is None:
            _say("status: plan")
            return 0
        return await _run(args, anz, plan, gas, outcome)


async def _run(
    args: argparse.Namespace,
    anz: Analyzer,
    plan: ManualCalibrationPlan,
    gas: CalibrationGas,
    outcome: _Outcome,
) -> int:
    rule = SteadinessRule(
        window_s=args.window,
        response_factor=args.response_factor,
        band_percent_fs=args.band,
        tolerance_percent_fs=args.tolerance,
        timeout_s=args.settle_timeout,
        max_gap_s=args.max_gap,
    )
    adc = not args.no_adc and (
        anz.session.availability.get(Capability.ADC_VALUES) is Availability.SUPPORTED
    )
    run = anz.manual_calibration(
        plan, gas=gas, confirm=True, rule=rule, adc=adc, interval=args.interval
    )
    named = gas.label or f"{gas.value:g} {gas.unit or ''}".strip()
    status = "refused"
    try:
        async with run:
            _say(
                f"On the {plan.kind.value} wait step of {', '.join(c.value for c in run.gases)}. "
                f"Switch the gas at the inlet to {named} now; waiting for the reading to settle "
                f"(up to {rule.timeout_s:g} s)."
            )
            status = await _decide(args, run)
    except FujiError as exc:
        _say(f"error: {exc}")
        status = "refused" if not run.keys else "stopped"
    finally:
        with anyio.CancelScope(shield=True):
            outcome.result = run.result
            if run.result is not None and run.result.event is not None:
                _report(run.result.event)
            await _record(args, anz, run)
    return _finish(status, run.result)


def _finish(status: str, result: RemoteCalibrationResult | None) -> int:
    """Say how the run ended; its exit code."""
    status = _status(status, result)
    _say("Switch the gas at the inlet back to the sample.")
    _say(f"status: {status}")
    return 0 if status == "completed" else 1


async def _decide(args: argparse.Namespace, run: RemoteCalibration) -> str:
    """Wait for the gas, ask, and calibrate; wait again if the gas moves before the key."""
    channels = ", ".join(c.value for c in run.gases)
    progress = _Progress()
    while True:
        verdict = await run.wait_steady(progress=progress)
        _say("Steady: " + "; ".join(verdict.reasons))
        question = f"Calibrate {channels} now? [y/N] "
        if not args.auto and not await _ask_reading(run, question, args.interval):
            await run.cancel()
            return "cancelled"
        try:
            await run.calibrate(confirm=True)
        except FujiAnalyzerStateError:
            # Refused, nothing sent. Only a gas that moved again is waited for once more.
            again = run.steadiness
            if run.state is not RunState.WAITING or again is None or again.steady:
                raise
            _say("The reading moved again before the key; waiting for it to settle.")
            continue
        return "calibrated"


def _status(status: str, result: RemoteCalibrationResult | None) -> str:
    """The ``status:`` word: the cleanup, then what the pass did, then ``status``."""
    if result is None:
        return status
    if not result.cleanup.clean:
        return "not_clean"
    outcome = result.outcome
    if outcome is None or status == "refused":
        return status
    if outcome is ManualCalibrationOutcome.CANCELLED:
        return "cancelled"
    return outcome.value


def _report(event: ManualCalibrationEvent) -> None:
    _say(f"The {event.kind.value} {event.outcome.value}: {'; '.join(event.evidence)}")
    for channel in event.channels:
        seen = (("before", event.before.get(channel)), ("after", event.after.get(channel)))
        parts = [f"  {channel.value}:"]
        parts += [f"{when} {r.value} {r.unit.value}" for when, r in seen if r is not None]
        deviation = event.deviations.get(channel)
        if deviation is not None:
            parts.append(f"deviation {deviation:+g}")
        _say(" ".join(parts))
    if event.adc_before is not None:
        _say(f"  detector counts: {list(event.adc_before.inputs)}")


async def _record(args: argparse.Namespace, anz: Analyzer, run: RemoteCalibration) -> None:
    """Write the run's record, and say where; nothing when the run never began."""
    result = run.result
    if result is None:
        return
    info = anz.info
    document = result.as_record(
        info=info,
        port=anz.port,
        address=anz.address,
        operator=args.operator,
        notes=args.notes,
    )
    path = args.out or Path(calibration_filename(result, info.serial_number if info else None))
    text = json.dumps(document, indent=2) + "\n"
    try:
        await anyio.to_thread.run_sync(partial(path.write_text, text, encoding="utf-8"))
    except OSError as exc:
        _say(f"error: the record could not be written to {path}: {exc}")
        return
    _say(f"record: {path}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``fuji-calibrate``; the module docstring gives its exit codes."""
    parser = _parser()
    args = parser.parse_args(argv)
    gas = _check(parser, args)

    outcome = _Outcome()

    async def body() -> int:
        with ctrl_break_as_ctrl_c():
            return await _calibrate(args, gas, outcome)

    try:
        return run_async_cli(body)
    except KeyboardInterrupt:
        result = outcome.result
        if result is None or result.cleanup.clean:
            sys.stderr.write("stopped by Ctrl-C\n")
        else:
            sys.stderr.write("stopped by Ctrl-C; the front panel was not left clean: check it\n")
        return _finish("cancelled", result)
