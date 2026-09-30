"""Operation commands: auto calibration, auto zero, blowback, return to measurement.

The four documented commands (design §6.2-§6.4) are FC06 writes of 1 to 42002-42005
(:data:`~fujilib.registry.write_policy.OPERATIONS`). This module plans them,
sends them and reports what they did; the gates belong to the session.

**What a calibration touches** (:func:`plan_calibration`, ZPA manual p.45-47,
p.53-60). Auto calibration and auto zero calibration act on the channels
enabled for auto calibration (40021-40025), each on its auto-calibration
range (40116-40120), and on both ranges where the calibration range is
"both" (40031-40035). The "at once" setting (40026-40030) does not widen
them: their zero is always done together. Auto calibration then spans the
channels one at a time from Ch1. The plan also gives the calibration gases
each range will be calibrated against, the flow times, whether the outputs
are held, and a duration that is **inferred** from the flow times, which the
manuals do not state as such.

**What a command did** (:class:`CommandResult`). The analyzer's reply means
it accepted the command, not that it finished (TN5A1190a p.11, p.17), and a
command is never retried. So the status is read after it, shielded from
cancellation and within its own deadline:

- a calibration is ``started`` when the status shows it running, and
  ``ambiguous`` when the analyzer acknowledged it but nothing runs: it never
  started, or already ended (design §6.4);
- return to measurement is ``done`` when the panel shows the measurement
  screen and no calibration flag is set. It is refused while a flag is set
  before it (design §13.1 #83): on a manual calibration's wait step 42002
  brings the display back but leaves the flag set (protocol findings §18.4);
- blowback is only ``sent``: no register shows it running.

A command whose reply was lost is established from the status where the
status can say (a calibration running, the measurement screen shown), and is
otherwise an unknown outcome.

No register stops a running calibration. The front panel can force-stop one,
but not while key lock is on (ZPA manual p.55-57, p.62).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio

from fujilib._deadline import Deadline
from fujilib.devices.reads import StatusRead, read_registers, read_status
from fujilib.errors import (
    ErrorContext,
    FujiAnalyzerStateError,
    FujiConnectionError,
    FujiDecodeError,
    FujiError,
    FujiTimeoutError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.modbus.client import FailureKind
from fujilib.registry.channels import MEASURED_CHANNELS, ChannelId
from fujilib.registry.enums import CalibrationRangeMode, DisplayScreen

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import ChannelStatus, DisplayState, RangeInfo, TransferTiming
    from fujilib.protocol.base import ProtocolClient
    from fujilib.registry.enums import ErrorCode
    from fujilib.registry.write_policy import OperationSpec

__all__ = [
    "PLAN_SETTINGS",
    "CalibrationPlan",
    "CalibrationRun",
    "CalibrationStatus",
    "CalibrationTarget",
    "CalibrationWait",
    "CommandOutcome",
    "CommandResult",
    "calibration_status",
    "check_healthy",
    "plan_calibration",
    "read_calibration_plan",
    "send_command",
]


class CalibrationRun(StrEnum):
    """Which automatic calibration a command starts."""

    AUTO_CALIBRATION = "auto_calibration"
    """Zero and span (42003)."""
    AUTO_ZERO = "auto_zero"
    """Zero only (42004)."""


class CommandOutcome(StrEnum):
    """What the status read after a command showed."""

    STARTED = "started"
    """The calibration is running."""
    AMBIGUOUS = "ambiguous"
    """Acknowledged, but nothing runs: it never started, or it already ended."""
    DONE = "done"
    """The front panel shows the measurement screen, and no calibration flag is set."""
    SENT = "sent"
    """Acknowledged; nothing shows what it did."""


# --- Status ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationStatus:
    """What is calibrating, held or failed, from one read of the status blocks."""

    running: bool
    """Input 30049: an auto calibration or auto zero calibration is running."""
    channels: Mapping[ChannelId, ChannelStatus]
    """Channels 1-5: calibration flags, hold, errors 4-9, current range."""
    calibration_error: bool
    """The calibration-error contact: an error 4-9 is active on some channel."""
    display: DisplayState | None
    read_at: datetime
    """Host UTC time the status was read."""

    @property
    def busy(self) -> bool:
        """Whether any calibration, automatic or manual, is running."""
        return self.running or any(s.calibrating for s in self.channels.values())

    @property
    def held(self) -> tuple[ChannelId, ...]:
        """Channels whose outputs are held."""
        return tuple(c for c, s in self.channels.items() if s.hold)

    @property
    def errors(self) -> Mapping[ChannelId, frozenset[ErrorCode]]:
        """Errors 4-9 active per channel; channels without any are left out."""
        return MappingProxyType({c: s.errors for c, s in self.channels.items() if s.errors})


def calibration_status(status: StatusRead) -> CalibrationStatus:
    """The calibration view of a status read."""
    return CalibrationStatus(
        running=status.analyzer.auto_calibration_running,
        channels=status.channels,
        calibration_error=status.analyzer.calibration_error,
        display=status.analyzer.display,
        read_at=status.timings[-1].received_at,
    )


def check_healthy(status: StatusRead, operation: str) -> None:
    """Refuse a calibration while the analyzer reports an analyzer-level error.

    A calibration now would compute its coefficients from a faulty measurement.

    Raises:
        FujiAnalyzerStateError: an instrument error or error 1, 2, 3 or 10 is active.
    """
    analyzer = status.analyzer
    if analyzer.instrument_error or analyzer.errors:
        codes = ", ".join(str(int(e)) for e in sorted(analyzer.errors)) or "unnumbered"
        msg = (
            f"{operation} refused, nothing was sent: the analyzer reports an instrument "
            f"error ({codes}), so a calibration now would be computed from a faulty reading"
        )
        raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=operation))


@dataclass(frozen=True, slots=True)
class CalibrationWait:
    """How :meth:`~fujilib.devices.analyzer.Analyzer.wait_for_calibration` ended."""

    final: CalibrationStatus
    """The status that showed nothing calibrating."""
    saw_running: bool
    """Whether any read showed a calibration running; if not, it may have ended already."""
    polls: int
    elapsed_s: float
    new_errors: Mapping[ChannelId, frozenset[ErrorCode]]
    """Errors 4-9 active at the end that were not in the baseline."""

    @property
    def failed(self) -> bool:
        """Whether any calibration error is active at the end.

        An error left by an earlier calibration counts too: the analyzer shows
        no difference. :attr:`new_errors` says which appeared since the baseline.
        """
        return bool(self.final.errors) or self.final.calibration_error


# --- Plans -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationTarget:
    """One channel an automatic calibration will calibrate."""

    channel: ChannelId
    ranges: tuple[int, ...]
    """The ranges calibrated, 1 and/or 2."""
    span: bool
    """Whether it is spanned as well as zeroed (auto calibration)."""
    widened: bool
    """Whether "both" adds the range beyond its auto-calibration range."""
    established: bool
    """Whether the session knows the channel is present."""
    zero_gas: tuple[float | None, ...]
    """The zero gas of each range in :attr:`ranges`, in its range's unit."""
    span_gas: tuple[float | None, ...]
    """The span gas of each range in :attr:`ranges`, in its range's unit."""
    units: tuple[str, ...]
    """The unit of each range in :attr:`ranges`."""


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    """What an automatic calibration will do, read from the settings (see the module docstring)."""

    run: CalibrationRun
    targets: tuple[CalibrationTarget, ...]
    hold: bool
    """Whether output hold is on: the outputs and Modbus concentrations are held throughout."""
    phases: tuple[tuple[str, int], ...]
    """``(phase, flow time in s)`` in order; which flow time is which is inferred."""
    estimated_duration_s: int
    """The sum of the phases' flow times; an inference, not a documented figure."""
    notes: tuple[str, ...]

    @property
    def channels(self) -> tuple[ChannelId, ...]:
        """The channels calibrated."""
        return tuple(t.channel for t in self.targets)


#: The settings a plan is made from.
PLAN_SETTINGS: Final[tuple[str, ...]] = (
    *(
        f"calibration_gas.ch{c}.range{r}.{k}"
        for c in range(1, 6)
        for r in (1, 2)
        for k in ("zero", "span")
    ),
    *(f"auto_calibration.ch{c}.included" for c in range(1, 6)),
    *(f"calibration.ch{c}.range_mode" for c in range(1, 6)),
    *(f"auto_calibration.ch{c}.range" for c in range(1, 6)),
    "output_hold.enabled",
    *(f"auto_calibration.flow_time{k}" for k in range(1, 8)),
    "auto_zero.flow_time",
)

_FLOW_NOTE: Final = (
    "The duration adds the flow times; which flow time belongs to which gas is inferred "
    "from the panel's flow-time screen, and the manuals state no processing time."
)


def plan_calibration(
    settings: Mapping[str, RegisterValue],
    run: CalibrationRun,
    *,
    ranges: Sequence[RangeInfo],
    established: Iterable[ChannelId],
) -> CalibrationPlan:
    """What ``run`` will do, from the :data:`PLAN_SETTINGS` values and the range tables."""
    known = set(established)
    counts = {info.channel: info.count for info in ranges}
    targets: list[CalibrationTarget] = []
    notes: list[str] = []
    span = run is CalibrationRun.AUTO_CALIBRATION

    def raw(name: str) -> int:
        value = settings[name].raw
        return int(value)

    for channel in MEASURED_CHANNELS:
        c = channel.number
        if not raw(f"auto_calibration.ch{c}.included"):
            continue
        own = raw(f"auto_calibration.ch{c}.range") + 1
        if own not in {1, 2}:
            msg = (
                f"auto_calibration.ch{c}.range reads {own - 1}, which is not a range; "
                "the plan cannot say what the calibration would touch"
            )
            raise FujiDecodeError(msg, context=ErrorContext(channel=channel.value))
        both = raw(f"calibration.ch{c}.range_mode") == CalibrationRangeMode.BOTH
        existing = counts.get(channel, 2)
        numbers = (1, 2)[:existing] if both else (own,)
        if own > existing:
            notes.append(
                f"{channel.value} is set to auto-calibrate range {own}, "
                f"but it has {existing} range(s)"
            )
        gases = [
            (
                settings[f"calibration_gas.ch{c}.range{r}.zero"],
                settings[f"calibration_gas.ch{c}.range{r}.span"],
            )
            for r in numbers
        ]
        targets.append(
            CalibrationTarget(
                channel=channel,
                ranges=numbers,
                span=span,
                widened=len(numbers) > 1,
                established=channel in known,
                zero_gas=tuple(_scaled(z) for z, _ in gases),
                span_gas=tuple(_scaled(s) for _, s in gases),
                units=tuple(z.unit or "?" for z, _ in gases),
            )
        )
    unknown = [t.channel.value for t in targets if not t.established]
    if unknown:
        notes.append(
            f"{', '.join(unknown)} enabled but not established: what the analyzer does with "
            "an enabled channel that is not fitted is not documented"
        )
    hold = bool(settings["output_hold.enabled"].raw)
    phases: list[tuple[str, int]]
    if span:
        phases = [("zero", raw("auto_calibration.flow_time1"))]
        phases += [
            (f"{t.channel.value} span", raw(f"auto_calibration.flow_time{t.channel.number + 1}"))
            for t in targets
        ]
        if hold:
            phases.append(("hold extension", raw("auto_calibration.flow_time7")))
    else:
        flow = raw("auto_zero.flow_time")
        phases = [("zero", flow)]
        if hold:
            phases.append(("gas replacement", flow))
    notes.append(_FLOW_NOTE)
    return CalibrationPlan(
        run=run,
        targets=tuple(targets),
        hold=hold,
        phases=tuple(phases),
        estimated_duration_s=sum(t for _, t in phases),
        notes=tuple(notes),
    )


def _scaled(value: RegisterValue) -> float | None:
    return value.value if isinstance(value.value, float) else None


async def read_calibration_plan(
    client: ProtocolClient,
    run: CalibrationRun,
    *,
    ranges: Sequence[RangeInfo],
    established: Iterable[ChannelId],
    deadline: Deadline | None = None,
) -> CalibrationPlan:
    """Read the :data:`PLAN_SETTINGS` and make the plan of ``run`` (two transactions)."""
    settings = await read_registers(client, PLAN_SETTINGS, ranges=ranges, deadline=deadline)
    return plan_calibration(settings, run, ranges=ranges, established=established)


# --- Sending a command -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    """An operation command sent once, and what the status after it showed."""

    operation: str
    outcome: CommandOutcome
    acknowledged: bool
    """Whether the analyzer's reply to the command arrived."""
    status: CalibrationStatus | None
    """The status read after it; ``None`` when that read failed."""
    plan: CalibrationPlan | None
    """For a calibration: what it was to do, read just before it was sent."""
    timing: TransferTiming | None
    """The command's timing, when its reply arrived."""
    status_error: FujiError | None = None
    """Why the status read after the command failed, when it did."""
    before: CalibrationStatus | None = None
    """The status read just before the command."""


async def send_command(
    client: ProtocolClient,
    operation: OperationSpec,
    *,
    plan: CalibrationPlan | None,
    deadline: Deadline,
    verify_timeout: float,
    before: CalibrationStatus | None = None,
) -> CommandResult:
    """Send ``operation`` once with FC06 and read the status after it (see the module docstring).

    Raises:
        FujiModbusError: the analyzer refused the command with an exception reply.
        FujiWriteOutcomeUnknownError: its reply was lost and the status cannot
            say whether it acted, or the port failed.
        FujiVerificationError: return to measurement was acknowledged, but the
            panel does not show the measurement screen; or the panel shows it
            with a calibration flag set.
        FujiError: the command could not be sent; nothing was.
    """
    name = operation.name
    acknowledged = False
    timing: TransferTiming | None = None
    try:
        timing = await client.write_register(
            operation.address, operation.value, deadline=deadline, command=name
        )
        acknowledged = True
    except FujiWriteOutcomeUnknownError as exc:
        if exc.context.extra.get("failure") == FailureKind.CONNECTION.value:
            raise
    after = await _read_status_after(client, verify_timeout, name)
    status = calibration_status(after) if isinstance(after, StatusRead) else None
    outcome = _outcome(name, status, acknowledged=acknowledged, read_error=after)
    error = after if isinstance(after, FujiError) else None
    return CommandResult(name, outcome, acknowledged, status, plan, timing, error, before)


async def _read_status_after(
    client: ProtocolClient, verify_timeout: float, command: str
) -> StatusRead | FujiError:
    # A shielded scope never swallows the block's end, but a checker cannot know that.
    outcome: StatusRead | FujiError = FujiTimeoutError("the status read did not run")
    with anyio.CancelScope(shield=True):
        deadline = Deadline.after(verify_timeout, operation=f"{command} status")
        try:
            outcome = await read_status(client, deadline=deadline)
        except FujiError as exc:
            outcome = exc
    return outcome


_CALIBRATIONS: Final = frozenset({"start_auto_calibration", "start_auto_zero_calibration"})


def _outcome(
    name: str,
    status: CalibrationStatus | None,
    *,
    acknowledged: bool,
    read_error: StatusRead | FujiError,
) -> CommandOutcome:
    context = ErrorContext(command_name=name, extra={"acknowledged": acknowledged})
    if isinstance(read_error, FujiConnectionError):
        context = context.merged(failure=FailureKind.CONNECTION.value)
    if status is not None:
        if name == "return_to_measurement":
            shown = status.display.screen if status.display is not None else None
            if shown == DisplayScreen.MEASUREMENT and status.busy:
                msg = (
                    f"{name}: the front panel shows the measurement screen, but a "
                    "calibration flag is set; a calibration began at the panel, or the "
                    "command left one open"
                )
                raise FujiVerificationError(msg, context=context)
            if shown == DisplayScreen.MEASUREMENT:
                return CommandOutcome.DONE
            if acknowledged:
                msg = (
                    f"{name}: acknowledged, but the front panel does not show the measurement "
                    "screen"
                )
                raise FujiVerificationError(msg, context=context)
        elif name in _CALIBRATIONS:
            if status.busy:
                return CommandOutcome.STARTED
            if acknowledged:
                return CommandOutcome.AMBIGUOUS
    if acknowledged:
        return CommandOutcome.SENT
    what = "cannot be read" if status is None else "does not show what it did"
    msg = (
        f"{name}: its reply was lost, and the status {what}; it may or may not have been "
        "carried out"
    )
    raise FujiWriteOutcomeUnknownError(msg, context=context.merged(write_state="unknown"))
