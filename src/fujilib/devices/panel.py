"""Manual calibration at the front panel: plans, watching and events (design §6.5).

A manual zero or span exists only at the front panel: ZERO or SPAN, the
cursor to the channel, ENT to select it (the wait step, while the gas
settles), and ENT again to calibrate. This module watches one from the status
registers; it sends no key.

**What the registers show** (protocol findings §14, observed on the bench
analyzer):

- the step (30182): 4 or 7 while a channel is selected, 5 or 8 while the gas
  settles, 6 or 9 while it runs, 10 on the error display, then 0. The screen
  (30181) stays on measurement throughout. On any other screen 30182 numbers
  the menu's pages instead, 4-10 among them (protocol findings §15), so it is
  read as a step only on the measurement screen;
- the per-channel zero and span flags (30050-30059), from the wait step to the end;
- 30186 (``display.calibration_result``, undocumented): 0 once a channel is
  selected, 4 while it runs, 6 when it has finished, kept until the next;
- 30190 (``display.key``, undocumented): the key being pressed.

**What a zero or span touches** (:func:`plan_manual_calibration`, ZPA manual
p.43-45, p.75-77). A zero of a channel set to "at once" zeroes every channel
so set; a span, or a zero of a channel set to "each", calibrates that channel
only. A channel set to "both" is calibrated on both its ranges; otherwise on
the range it measures on or, when its range method is auto, on its
auto-calibration range.

**How an event is decided** (:class:`ManualCalibrationTracker`). A pass
starts when the step leaves 0 and ends when it is 0 again, or when a read
shows a screen other than measurement. A poll reads the
readings and the zero and span flags before the step, so when the first read
back on measurement still shows the pass's flags, the readings after it are
taken from the next read.

- It *ran* if a read showed it running or on the error display, or if the
  result register read 0 on the wait step and 6 at the end: a calibration
  takes a second or two, which can fall between two reads.
- It *failed* if the error display was shown, or an error 4-8 appeared on its
  channels.
- It was *cancelled* if it never reached the wait step, or if it did and the
  result register still reads 0 at the end.
- Anything else is *ambiguous*: the reads do not say whether it ran.

The result register is undocumented, so it decides nothing that the step
contradicts (design §13.1 #73). Every event carries the evidence for its
outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.devices.operations import CalibrationTarget
from fujilib.errors import ErrorContext, FujiDecodeError, FujiValidationError
from fujilib.registry.channels import MEASURED_CHANNELS, ChannelId
from fujilib.registry.enums import (
    CalibrationRangeMode,
    DisplayScreen,
    ManualCalibrationResult,
    ManualCalibrationStep,
    RangeMethod,
    ZeroCalibrationMode,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import (
        AdcValues,
        ChannelStatus,
        DisplayState,
        Frame,
        RangeInfo,
        Reading,
    )
    from fujilib.devices.operations import CalibrationStatus
    from fujilib.registry.enums import ErrorCode

__all__ = [
    "MANUAL_PLAN_SETTINGS",
    "ManualCalibrationEvent",
    "ManualCalibrationKind",
    "ManualCalibrationOutcome",
    "ManualCalibrationPlan",
    "ManualCalibrationTracker",
    "PanelObservation",
    "plan_manual_calibration",
]


class ManualCalibrationKind(StrEnum):
    """A manual zero or a manual span."""

    ZERO = "zero"
    SPAN = "span"


class ManualCalibrationOutcome(StrEnum):
    """How a manual calibration ended, as far as the reads can tell."""

    COMPLETED = "completed"
    """It ran, and no calibration error followed."""
    FAILED = "failed"
    """It ran into the error display, or an error 4-8 appeared on its channels."""
    CANCELLED = "cancelled"
    """It was left before it ran."""
    AMBIGUOUS = "ambiguous"
    """The reads do not say whether it ran."""


_STEP = ManualCalibrationStep
_SELECT: Final = frozenset({_STEP.ZERO_CHANNEL_SELECT, _STEP.SPAN_CHANNEL_SELECT})
_WAIT: Final = frozenset({_STEP.ZERO_WAIT, _STEP.SPAN_WAIT})
_RUNNING: Final = frozenset({_STEP.ZERO_RUNNING, _STEP.SPAN_RUNNING})
_ZERO_STEPS: Final = frozenset({_STEP.ZERO_CHANNEL_SELECT, _STEP.ZERO_WAIT, _STEP.ZERO_RUNNING})
_SPAN_STEPS: Final = frozenset({_STEP.SPAN_CHANNEL_SELECT, _STEP.SPAN_WAIT, _STEP.SPAN_RUNNING})
#: Errors that a calibration itself can raise (error 9 is auto calibration's).
_CALIBRATION_ERRORS: Final = range(4, 9)


# --- Observations ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelObservation:
    """One read of the panel and calibration status, with the host time it was read."""

    at: datetime
    """Host UTC time of the status read."""
    display: DisplayState
    channels: Mapping[ChannelId, ChannelStatus]
    """Status of the measured channels read."""
    calibration_error: bool
    readings: Mapping[ChannelId, Reading] = field(default_factory=lambda: MappingProxyType({}))
    """The readings of the same poll, when it was a poll."""
    adc: AdcValues | None = None
    """The raw A/D values read with it, if any."""

    @classmethod
    def from_frame(cls, frame: Frame, *, adc: AdcValues | None = None) -> PanelObservation | None:
        """The observation in a poll's frame; ``None`` for a poll without the status block."""
        analyzer = frame.analyzer
        if analyzer is None or analyzer.display is None:
            return None
        timing = frame.status_timing if frame.status_timing is not None else frame.readings_timing
        return cls(
            at=timing.received_at,
            display=analyzer.display,
            channels=MappingProxyType(
                {r.channel: r.status for r in frame.readings if r.status is not None}
            ),
            calibration_error=analyzer.calibration_error,
            readings=MappingProxyType({r.channel: r for r in frame.readings}),
            adc=adc,
        )

    @classmethod
    def from_status(cls, status: CalibrationStatus) -> PanelObservation | None:
        """The observation in a calibration status; ``None`` when it has no display state."""
        if status.display is None:
            return None
        return cls(
            at=status.read_at,
            display=status.display,
            channels=status.channels,
            calibration_error=status.calibration_error,
        )

    @property
    def step(self) -> ManualCalibrationStep | int:
        """The manual-calibration step shown; ``NONE`` on any screen but measurement.

        A manual calibration keeps the measurement screen. In the menus 30182
        numbers the pages instead, 4-10 among them (protocol findings §15), so
        it is no step there.
        """
        if self.display.screen != DisplayScreen.MEASUREMENT:
            return ManualCalibrationStep.NONE
        return self.display.calibration_step

    def errors(self) -> dict[ChannelId, frozenset[ErrorCode]]:
        """Errors 4-9 active per channel read."""
        return {c: s.errors for c, s in self.channels.items()}


# --- Events ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManualCalibrationEvent:
    """A manual zero or span made at the front panel, and how it ended (design §8)."""

    kind: ManualCalibrationKind
    outcome: ManualCalibrationOutcome
    channels: tuple[ChannelId, ...]
    """The channels calibrated: those whose zero or span flag was set, else the cursor's."""
    ranges: Mapping[ChannelId, int]
    """Each channel's current range when it was calibrated."""
    started_at: datetime
    """The first read that showed the zero or span screen."""
    selected_at: datetime | None
    """The first read on the wait step: the channel was selected and the gas supplied."""
    ran_after: datetime | None
    """It ran after this read (the last on the wait step), when it ran."""
    ran_before: datetime | None
    """It ran before this read (the first that showed it running or done), when it ran."""
    ended_at: datetime
    """The first read that showed no step: back on the measurement screen, or on a menu."""
    before: Mapping[ChannelId, Reading]
    """The channels' readings at ``ran_after``: what the calibration saw."""
    after: Mapping[ChannelId, Reading]
    """The channels' readings at ``ended_at``."""
    adc_before: AdcValues | None
    """The raw A/D values read with ``before``: the detectors' counts when it ran."""
    new_errors: Mapping[ChannelId, frozenset[ErrorCode]]
    """Errors 4-9 on its channels at the end that were not active before it."""
    evidence: tuple[str, ...]
    """Why the outcome is what it is, and anything odd the reads showed."""
    observations: int
    """How many reads the pass spanned."""
    gases: Mapping[ChannelId, float | None] = field(default_factory=lambda: MappingProxyType({}))
    """The calibration gas of each channel's range, in its unit, when it was read."""

    @property
    def ran(self) -> bool:
        """Whether it ran: completed or failed."""
        return self.outcome in {ManualCalibrationOutcome.COMPLETED, ManualCalibrationOutcome.FAILED}

    @property
    def calibrated_at(self) -> datetime | None:
        """When it ran, at the latest; ``None`` when it did not run or may not have."""
        if not self.ran:
            return None
        return self.ran_before if self.ran_before is not None else self.ended_at

    @property
    def deviations(self) -> Mapping[ChannelId, float]:
        """Each channel's reading before it ran, less its calibration gas.

        Empty unless it ran; only channels whose reading and gas are both
        known; in the reading's own unit, which a calibration gas shares
        (design §13.1 #62).
        """
        result: dict[ChannelId, float] = {}
        if not self.ran:
            return MappingProxyType(result)
        for channel, gas in self.gases.items():
            reading = self.before.get(channel)
            if gas is not None and reading is not None and reading.value is not None:
                result[channel] = round(reading.value - gas, reading.decimals)
        return MappingProxyType(result)


@dataclass(slots=True)
class _Pass:
    """What the reads of one pass through the calibration steps have shown."""

    kind: ManualCalibrationKind | None
    started_at: datetime
    baseline: Mapping[ChannelId, frozenset[ErrorCode]]
    selected_at: datetime | None = None
    last_wait: PanelObservation | None = None
    ran_after: datetime | None = None
    ran_before: datetime | None = None
    saw_running: bool = False
    saw_error_display: bool = False
    result_reset: bool = False
    flagged: set[ChannelId] = field(default_factory=set[ChannelId])
    cursor: ChannelId | None = None
    evidence: list[str] = field(default_factory=list[str])
    observations: int = 0
    ending: PanelObservation | None = None
    """The first read back on measurement, while its channels' flags still showed."""

    def ran_between(self, observation: PanelObservation) -> None:
        """Note that ``observation`` is the first to show that it ran."""
        if self.ran_before is None:
            self.ran_after = self.last_wait.at if self.last_wait is not None else None
            self.ran_before = observation.at


def _kind_of(step: ManualCalibrationStep | int) -> ManualCalibrationKind | None:
    if step in _ZERO_STEPS:
        return ManualCalibrationKind.ZERO
    if step in _SPAN_STEPS:
        return ManualCalibrationKind.SPAN
    return None


def _in_pass(step: ManualCalibrationStep | int) -> bool:
    return step in _ZERO_STEPS or step in _SPAN_STEPS or step == _STEP.ERROR_DISPLAY


class ManualCalibrationTracker:
    """Turns successive panel observations into manual calibration events.

    Feed it every observation in order, from polls, status reads or recorded
    frames; the closer together they are, the less is left ambiguous. It does
    no I/O.
    """

    def __init__(self) -> None:
        """Start with no pass under way."""
        self._pass: _Pass | None = None
        self._last: PanelObservation | None = None

    @property
    def active(self) -> bool:
        """Whether a pass through the calibration steps is under way."""
        return self._pass is not None

    @property
    def step(self) -> ManualCalibrationStep | int | None:
        """The step the last observation showed (``NONE`` on a menu); ``None`` before the first."""
        return self._last.step if self._last is not None else None

    def feed(self, observation: PanelObservation) -> ManualCalibrationEvent | None:
        """Take in ``observation``; return the event of a pass it ends, if it ends one."""
        step = observation.step
        event: ManualCalibrationEvent | None = None
        current = self._pass
        if current is not None:
            kind = _kind_of(step)
            if current.ending is not None:
                event = self._finish(current, observation)
                self._pass = self._start(observation) if _in_pass(step) else None
            elif step == _STEP.NONE:
                if _flagged(observation, current.kind) & current.flagged:
                    # A poll reads the readings and flags before the step, so this
                    # read's readings may predate the end: take them from the next.
                    current.ending = observation
                    current.observations += 1
                else:
                    event = self._finish(current, observation)
                    self._pass = None
            elif kind is not None and current.kind is not None and kind is not current.kind:
                event = self._finish(current, observation)
                note = "the next pass began before a read showed the measurement screen"
                event = replace(event, evidence=(*event.evidence, note))
                self._pass = self._start(observation)
            else:
                self._update(current, observation)
        elif _in_pass(step):
            self._pass = self._start(observation)
        self._last = observation
        return event

    def _start(self, observation: PanelObservation) -> _Pass:
        baseline = self._last.errors() if self._last is not None else observation.errors()
        current = _Pass(
            kind=_kind_of(observation.step),
            started_at=observation.at,
            baseline=baseline,
        )
        if observation.step not in _SELECT:
            current.evidence.append(
                f"first seen on step {_step_name(observation.step)}, not on channel selection"
            )
        self._update(current, observation)
        return current

    def _update(self, current: _Pass, observation: PanelObservation) -> None:
        current.observations += 1
        step = observation.step
        result = observation.display.calibration_result
        if current.kind is None:
            current.kind = _kind_of(step) or _kind_from_flags(observation)
        if step in _SELECT and observation.display.cursor_channel is not None:
            current.cursor = observation.display.cursor_channel
        if step in _WAIT:
            if current.selected_at is None:
                current.selected_at = observation.at
            current.last_wait = observation
            if result == ManualCalibrationResult.NONE:
                current.result_reset = True
        elif step in _RUNNING:
            current.saw_running = True
            current.ran_between(observation)
        elif step == _STEP.ERROR_DISPLAY:
            current.saw_error_display = True
            current.ran_between(observation)
        elif not _in_pass(step):
            current.evidence.append(f"an undocumented step {int(step)} was shown")
        if current.result_reset and result in {
            ManualCalibrationResult.RUNNING,
            ManualCalibrationResult.COMPLETED,
        }:
            current.ran_between(observation)
        current.flagged |= _flagged(observation, current.kind)

    def _finish(self, current: _Pass, end: PanelObservation) -> ManualCalibrationEvent:
        current.observations += 1
        evidence = list(current.evidence)
        channels = tuple(sorted(current.flagged, key=lambda c: c.number))
        if not channels and current.cursor is not None:
            channels = (current.cursor,)
            evidence.append("no zero or span flag was seen set; the channel is the cursor's")
        result = end.display.calibration_result
        if current.result_reset and result == ManualCalibrationResult.COMPLETED:
            current.ran_between(end)
        new_errors = _new_errors(current.baseline, end, channels)
        failed_codes = sorted(
            {int(e) for codes in new_errors.values() for e in codes if e in _CALIBRATION_ERRORS}
        )
        ran = current.ran_before is not None
        outcome = _outcome(current, ran=ran, result=result, failed_codes=failed_codes)
        evidence.extend(_reasons(current, outcome, result=result, failed_codes=failed_codes))
        screen = (current.ending or end).display.screen
        if screen != DisplayScreen.MEASUREMENT:
            evidence.append(f"the first read after it showed {_screen_name(screen)}")
        wait = current.last_wait
        kind = current.kind if current.kind is not None else ManualCalibrationKind.ZERO
        if current.kind is None:
            evidence.append("neither a zero nor a span step or flag was seen; taken as a zero")
        return ManualCalibrationEvent(
            kind=kind,
            outcome=outcome,
            channels=channels,
            ranges=MappingProxyType(
                {c: s.range for c, s in _statuses(wait, end).items() if c in channels}
            ),
            started_at=current.started_at,
            selected_at=current.selected_at,
            ran_after=current.ran_after if ran else None,
            ran_before=current.ran_before if ran else None,
            ended_at=(current.ending or end).at,
            before=MappingProxyType({c: r for c, r in _readings(wait).items() if c in channels}),
            after=MappingProxyType({c: r for c, r in end.readings.items() if c in channels}),
            adc_before=wait.adc if wait is not None else None,
            new_errors=MappingProxyType(new_errors),
            evidence=tuple(evidence),
            observations=current.observations,
        )


def _outcome(
    current: _Pass,
    *,
    ran: bool,
    result: ManualCalibrationResult | int | None,
    failed_codes: Sequence[int],
) -> ManualCalibrationOutcome:
    if current.saw_error_display or (failed_codes and (ran or current.selected_at is not None)):
        return ManualCalibrationOutcome.FAILED
    if ran:
        return ManualCalibrationOutcome.COMPLETED
    if current.selected_at is None:
        return ManualCalibrationOutcome.CANCELLED
    if current.result_reset and result == ManualCalibrationResult.NONE:
        return ManualCalibrationOutcome.CANCELLED
    return ManualCalibrationOutcome.AMBIGUOUS


def _reasons(
    current: _Pass,
    outcome: ManualCalibrationOutcome,
    *,
    result: ManualCalibrationResult | int | None,
    failed_codes: Sequence[int],
) -> list[str]:
    reasons: list[str] = []
    if current.saw_error_display:
        reasons.append("the error display was shown")
    if failed_codes and outcome is ManualCalibrationOutcome.FAILED:
        codes = ", ".join(map(str, failed_codes))
        reasons.append(f"calibration error(s) {codes} appeared on its channels")
    if current.saw_running:
        reasons.append("a read showed it running")
    elif current.ran_before is not None and not current.saw_error_display:
        reasons.append("the result register went from 0 on the wait step to 4 or 6")
    if current.saw_running and result == ManualCalibrationResult.NONE:
        reasons.append("the result register reads 0 although a read showed it running")
    if outcome is ManualCalibrationOutcome.CANCELLED:
        if current.selected_at is None:
            reasons.append("it never reached the wait step, so no gas was selected")
        else:
            reasons.append("it reached the wait step, and the result register still reads 0")
    elif outcome is ManualCalibrationOutcome.AMBIGUOUS:
        reasons.append(
            "it reached the wait step, no read showed it running, and the result "
            f"register ({_result_name(result)}) does not say whether it ran"
        )
    return reasons


def _kind_from_flags(observation: PanelObservation) -> ManualCalibrationKind | None:
    if any(s.zero_calibrating for s in observation.channels.values()):
        return ManualCalibrationKind.ZERO
    if any(s.span_calibrating for s in observation.channels.values()):
        return ManualCalibrationKind.SPAN
    return None


def _flagged(observation: PanelObservation, kind: ManualCalibrationKind | None) -> set[ChannelId]:
    zero = kind is not ManualCalibrationKind.SPAN
    span = kind is not ManualCalibrationKind.ZERO
    return {
        c
        for c, s in observation.channels.items()
        if (zero and s.zero_calibrating) or (span and s.span_calibrating)
    }


def _readings(observation: PanelObservation | None) -> Mapping[ChannelId, Reading]:
    return observation.readings if observation is not None else MappingProxyType({})


def _statuses(
    wait: PanelObservation | None, end: PanelObservation
) -> Mapping[ChannelId, ChannelStatus]:
    return wait.channels if wait is not None else end.channels


def _new_errors(
    baseline: Mapping[ChannelId, frozenset[ErrorCode]],
    end: PanelObservation,
    channels: Iterable[ChannelId],
) -> dict[ChannelId, frozenset[ErrorCode]]:
    wanted = set(channels) or set(end.channels)
    result: dict[ChannelId, frozenset[ErrorCode]] = {}
    for channel, status in end.channels.items():
        fresh = status.errors - baseline.get(channel, frozenset())
        if channel in wanted and fresh:
            result[channel] = fresh
    return result


def _step_name(step: ManualCalibrationStep | int) -> str:
    return step.name.lower() if isinstance(step, ManualCalibrationStep) else str(int(step))


def _screen_name(screen: DisplayScreen | int) -> str:
    if isinstance(screen, DisplayScreen):
        return f"the {screen.name.lower().replace('_', ' ')} screen"
    return f"undocumented screen {int(screen)}"


def _result_name(result: ManualCalibrationResult | int | None) -> str:
    if result is None:
        return "not read"
    return result.name.lower() if isinstance(result, ManualCalibrationResult) else str(result)


# --- Plans -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManualCalibrationPlan:
    """What a manual zero or span of one channel at the panel would calibrate."""

    kind: ManualCalibrationKind
    channel: ChannelId
    """The channel the cursor selects."""
    targets: tuple[CalibrationTarget, ...]
    """Every channel it calibrates, with its ranges and calibration gases."""
    notes: tuple[str, ...]

    @property
    def channels(self) -> tuple[ChannelId, ...]:
        """The channels calibrated."""
        return tuple(t.channel for t in self.targets)


_MEASURED_NUMBERS: Final = range(1, len(MEASURED_CHANNELS) + 1)

#: The settings a manual calibration plan is made from.
MANUAL_PLAN_SETTINGS: Final[tuple[str, ...]] = (
    *(
        f"calibration_gas.ch{c}.range{r}.{k}"
        for c in _MEASURED_NUMBERS
        for r in (1, 2)
        for k in ("zero", "span")
    ),
    *(f"calibration.ch{c}.zero_mode" for c in _MEASURED_NUMBERS),
    *(f"calibration.ch{c}.range_mode" for c in _MEASURED_NUMBERS),
    *(f"range.ch{c}.method" for c in _MEASURED_NUMBERS),
    *(f"auto_calibration.ch{c}.range" for c in _MEASURED_NUMBERS),
    *(f"range.ch{c}.current" for c in _MEASURED_NUMBERS),
)


def plan_manual_calibration(
    settings: Mapping[str, RegisterValue],
    kind: ManualCalibrationKind,
    channel: ChannelId,
    *,
    ranges: Sequence[RangeInfo],
    established: Iterable[ChannelId],
) -> ManualCalibrationPlan:
    """What a manual ``kind`` of ``channel`` would calibrate (see the module docstring).

    ``settings`` holds the :data:`MANUAL_PLAN_SETTINGS`.

    Raises:
        FujiValidationError: ``channel`` is not a measured channel (1-5).
        FujiDecodeError: a range setting reads a value that is not a range.
    """
    if not channel.is_measured:
        msg = f"{channel.value} is not a measured channel; only channels 1-5 are calibrated"
        raise FujiValidationError(msg, context=ErrorContext(channel=channel.value))
    known = set(established)
    counts = {info.channel: info.count for info in ranges}
    notes: list[str] = []

    def raw(name: str) -> int:
        return int(settings[name].raw)

    def at_once(c: ChannelId) -> bool:
        return raw(f"calibration.ch{c.number}.zero_mode") == ZeroCalibrationMode.AT_ONCE

    group: list[ChannelId] = [channel]
    if kind is ManualCalibrationKind.ZERO and at_once(channel):
        group = [c for c in MEASURED_CHANNELS if at_once(c)]
        if len(group) > 1:
            notes.append(
                f"{channel.value} is set to zero 'at once' with "
                f"{', '.join(c.value for c in group if c is not channel)}: they are zeroed together"
            )
    targets: list[CalibrationTarget] = []
    for c in group:
        n = c.number
        existing = counts.get(c, 2)
        if raw(f"calibration.ch{n}.range_mode") == CalibrationRangeMode.BOTH:
            numbers: tuple[int, ...] = (1, 2)[:existing]
        elif raw(f"range.ch{n}.method") == RangeMethod.AUTO:
            numbers = (_range_number(raw(f"auto_calibration.ch{n}.range"), c, "auto_calibration"),)
            notes.append(
                f"{c.value} switches range automatically, so it is calibrated on its "
                f"auto-calibration range, range {numbers[0]} (ZPA manual p.76)"
            )
        else:
            numbers = (_range_number(raw(f"range.ch{n}.current"), c, "range"),)
        gases = [
            (
                settings[f"calibration_gas.ch{n}.range{r}.zero"],
                settings[f"calibration_gas.ch{n}.range{r}.span"],
            )
            for r in numbers
        ]
        targets.append(
            CalibrationTarget(
                channel=c,
                ranges=numbers,
                span=kind is ManualCalibrationKind.SPAN,
                widened=len(numbers) > 1,
                established=c in known,
                zero_gas=tuple(_scaled(z) for z, _ in gases),
                span_gas=tuple(_scaled(s) for _, s in gases),
                units=tuple(z.unit or "?" for z, _ in gases),
            )
        )
    unknown = [t.channel.value for t in targets if not t.established]
    if unknown:
        notes.append(
            f"{', '.join(unknown)} would be calibrated but is not established: whether it is "
            "fitted is not known"
        )
    return ManualCalibrationPlan(kind, channel, tuple(targets), tuple(notes))


def _range_number(raw: int, channel: ChannelId, what: str) -> int:
    if raw not in {0, 1}:
        msg = (
            f"{what}.ch{channel.number} reads {raw}, which is not a range; the plan cannot say "
            "what the calibration would touch"
        )
        raise FujiDecodeError(msg, context=ErrorContext(channel=channel.value))
    return raw + 1


def _scaled(value: RegisterValue) -> float | None:
    return value.value if isinstance(value.value, float) else None
