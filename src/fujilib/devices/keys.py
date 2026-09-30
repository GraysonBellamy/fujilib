"""Front-panel keys over Modbus, and a manual zero or span driven with them (design §6.5).

A manual zero or span exists only as front-panel keys: ZERO or SPAN, the
cursor to the channel, ENT to select it (the wait step, while the gas
settles) and ENT again to calibrate. 42001 presses a key as the panel would
(protocol findings §18). This module is the only one that writes it.

**Which keys, and where.** Only UP, DOWN, ESC, ENT, ZERO and SPAN, never MODE
or SIDE, which open the menus and enter their passwords; the write envelope
checks the value written as well as the address (design §5.4). Each key only
on the steps where it belongs (:func:`key_refusal`):

| Step | Keys |
|---|---|
| measurement, no calibration flag set | ZERO, SPAN |
| channel selection | UP, DOWN, ESC; ENT with the cursor on the planned channel |
| wait | ESC; ENT, which calibrates, only through :meth:`RemoteCalibration.calibrate` |
| running | none |
| error display | ESC; never ENT, which forces the calibration on errors 5 and 7 |
| any other screen | none |

**One key is one locked operation:** read the panel and refuse unless the
step allows the key; write it once, never retried; read until the step, the
cursor and the flags show it taken. 30190 cannot confirm a key written over
Modbus: it shows only keys pressed at the panel (findings §18.1). A key the
panel does not take (key lock, the backlight, a key pressed at the panel
meanwhile) stops the run.

**The cursor** wraps round, and channels zeroed "at once" share a position
that reads as its first channel reached going down (findings §18.2). So the
cursor is moved with DOWN only, one key at a time, each confirmed by the
cursor moving, until it reads the planned channel, or the first of the "at
once" channels. ZERO may open it anywhere, so it is read, never predicted.

**A remote calibration** (:class:`RemoteCalibration`, from
:meth:`Analyzer.manual_calibration <fujilib.devices.analyzer.Analyzer.manual_calibration>`)
is an async context manager. Entering it refuses (nothing sent) unless the
panel is on the measurement screen with no calibration flag set, key lock
and output hold are off (design §13.1 #85, #86), there is no instrument
error, the plan reads the same again and calibrates no channel on both
ranges (#88), and the operator's gas for every channel is its calibration-gas
setting (#89). It then presses ZERO or SPAN, moves the cursor and selects the
channel. Inside, :meth:`~RemoteCalibration.wait_steady` watches the readings
until the steadiness rule holds (design §13.1 #78), and
:meth:`~RemoteCalibration.calibrate` checks it all again and sends the ENT
that calibrates, which is ``DANGEROUS``. The other keys are ``STATEFUL``.

**Cleanup.** However the block is left, the panel is returned to measurement
for the step it is on, shielded from cancellation and within its own time:

| Step | Cleanup |
|---|---|
| channel selection | ESC |
| wait | ESC, which clears the flags; never 42002, which leaves them set (findings §18.4) |
| running | wait for the analyzer to finish |
| error display | ESC |
| any other screen | 42002 |

Each cleanup key is sent once. A flag still set on the measurement screen
afterwards raises :class:`~fujilib.errors.FujiAnalyzerStateError` naming the
channel and the recovery: enter its wait step at the panel and press ESC.
fujilib does not try that itself (design §13.1 #84).

The run is recorded as the watcher records one made at the panel: the
:class:`~fujilib.devices.panel.ManualCalibrationTracker` sees every read, and
:class:`RemoteCalibrationResult` adds the plan, the gases named, the
steadiness, the readings on the wait step, the keys and the cleanup.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Self, cast

import anyio

from fujilib._deadline import Deadline
from fujilib._logging import get_logger
from fujilib.devices import panel, reads
from fujilib.devices.capability import Capability, SafetyTier
from fujilib.devices.operations import check_healthy
from fujilib.devices.panel import (
    ManualCalibrationKind,
    ManualCalibrationOutcome,
    ManualCalibrationTracker,
    PanelObservation,
)
from fujilib.devices.steadiness import SteadinessJudge, SteadinessRule, SteadinessTarget
from fujilib.devices.writes import calibrating_reasons
from fujilib.errors import (
    ErrorContext,
    FujiAnalyzerStateError,
    FujiConfigurationError,
    FujiConnectionError,
    FujiError,
    FujiModbusError,
    FujiTimeoutError,
    FujiValidationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.modbus.client import FailureKind
from fujilib.registry.channels import ChannelId, Gas, coerce_channel
from fujilib.registry.enums import (
    DisplayScreen,
    KeyCode,
    ManualCalibrationStep,
)
from fujilib.registry.units import Unit, coerce_unit
from fujilib.registry.write_policy import CALIBRATION_KEYS, KEY_SIMULATION_ADDRESS, OPERATIONS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from types import TracebackType

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import DeviceInfo, RangeInfo
    from fujilib.devices.panel import ManualCalibrationEvent, ManualCalibrationPlan
    from fujilib.devices.reads import StatusRead
    from fujilib.devices.session import Session
    from fujilib.devices.steadiness import SteadinessVerdict
    from fujilib.protocol.base import ProtocolClient

__all__ = [
    "CalibrationGas",
    "CleanupReport",
    "KeyPress",
    "RemoteCalibration",
    "RemoteCalibrationResult",
    "RunState",
    "WaitSample",
    "calibration_filename",
    "key_refusal",
]

_LOG = get_logger("keys")

_STEP = ManualCalibrationStep
_SELECT: Final = MappingProxyType(
    {
        ManualCalibrationKind.ZERO: _STEP.ZERO_CHANNEL_SELECT,
        ManualCalibrationKind.SPAN: _STEP.SPAN_CHANNEL_SELECT,
    }
)
_WAIT: Final = MappingProxyType(
    {ManualCalibrationKind.ZERO: _STEP.ZERO_WAIT, ManualCalibrationKind.SPAN: _STEP.SPAN_WAIT}
)
_OPEN: Final = MappingProxyType(
    {ManualCalibrationKind.ZERO: KeyCode.ZERO, ManualCalibrationKind.SPAN: KeyCode.SPAN}
)
_SELECT_STEPS: Final = frozenset(_SELECT.values())
_WAIT_STEPS: Final = frozenset(_WAIT.values())
_RUNNING_STEPS: Final = frozenset({_STEP.ZERO_RUNNING, _STEP.SPAN_RUNNING})
_NAVIGATION: Final = frozenset({KeyCode.UP, KeyCode.DOWN, KeyCode.ESC, KeyCode.ENT})
#: The steps the cleanup acts on with a key or a wait; any other takes 42002.
_CLEANUP: Final = frozenset(
    {_STEP.NONE, *_SELECT_STEPS, *_WAIT_STEPS, *_RUNNING_STEPS, _STEP.ERROR_DISPLAY}
)
#: The keys each step allows; ENT on a wait step only to calibrate, and on
#: channel selection only with the cursor on the planned channel.
_ALLOWED: Final[Mapping[ManualCalibrationStep | int, frozenset[KeyCode]]] = MappingProxyType(
    {
        _STEP.NONE: frozenset({KeyCode.ZERO, KeyCode.SPAN}),
        _STEP.ZERO_CHANNEL_SELECT: _NAVIGATION,
        _STEP.SPAN_CHANNEL_SELECT: _NAVIGATION,
        _STEP.ZERO_WAIT: frozenset({KeyCode.ESC, KeyCode.ENT}),
        _STEP.SPAN_WAIT: frozenset({KeyCode.ESC, KeyCode.ENT}),
        _STEP.ERROR_DISPLAY: frozenset({KeyCode.ESC}),
    }
)

#: The keys fujilib sends, by name; each is one of the write envelope's calibration keys.
KEY_NAMES: Final[Mapping[KeyCode, str]] = MappingProxyType(
    {
        KeyCode.UP: "UP",
        KeyCode.DOWN: "DOWN",
        KeyCode.ESC: "ESC",
        KeyCode.ENT: "ENT",
        KeyCode.ZERO: "ZERO",
        KeyCode.SPAN: "SPAN",
    }
)
if frozenset(int(k) for k in KEY_NAMES) != CALIBRATION_KEYS:  # pragma: no cover - at import
    _msg = "the driver's keys are not the write envelope's calibration keys"
    raise FujiConfigurationError(_msg)

#: The settings a remote calibration reads before its first key and before the ENT
#: that calibrates: the plan's, key lock, output hold and the response times.
_RUN_SETTINGS: Final[tuple[str, ...]] = (
    *panel.MANUAL_PLAN_SETTINGS,
    "key_lock",
    "output_hold.enabled",
    "response_time.o2",
    *(f"response_time.ndir{k}" for k in range(1, 5)),
)

_CONFIRM_POLL_S: Final = 0.05
_FOLLOW_POLL_S: Final = 0.2
_CLEANUP_STEPS: Final = 8

_OPERATION: Final = "manual_calibration"
_CALIBRATE: Final = "calibrate"


def _name(key: KeyCode) -> str:
    return KEY_NAMES.get(key, f"0x{int(key):02X}")


def _step_name(step: ManualCalibrationStep | int) -> str:
    if isinstance(step, ManualCalibrationStep):
        return step.name.lower().replace("_", " ")
    return f"step {int(step)}"


def _screen_name(screen: DisplayScreen | int) -> str:
    if isinstance(screen, DisplayScreen):
        return f"the {screen.name.lower().replace('_', ' ')} screen"
    return f"screen {int(screen)}"


def _flagged(observation: PanelObservation, kind: ManualCalibrationKind) -> frozenset[ChannelId]:
    zero = kind is ManualCalibrationKind.ZERO
    return frozenset(
        c
        for c, s in observation.channels.items()
        if (s.zero_calibrating if zero else s.span_calibrating)
    )


def _any_flag(observation: PanelObservation) -> frozenset[ChannelId]:
    return frozenset(
        c for c, s in observation.channels.items() if s.zero_calibrating or s.span_calibrating
    )


# --- One key ---------------------------------------------------------------------------------


def key_refusal(
    key: KeyCode,
    observation: PanelObservation,
    *,
    cursor: ChannelId | None = None,
    calibrate: bool = False,
) -> str | None:
    """Why ``key`` may not be sent on the panel ``observation`` shows; ``None`` if it may.

    ``cursor`` is where ENT on channel selection needs the cursor. ENT on a
    wait step starts the calibration, so it is allowed only with
    ``calibrate``. ENT on the error display is never allowed.
    """
    name = KEY_NAMES.get(key)
    if name is None:
        return f"0x{int(key):02X} is not one of the calibration keys"
    display = observation.display
    if display.screen != DisplayScreen.MEASUREMENT:
        return f"the panel shows {_screen_name(display.screen)}, not the measurement screen"
    step = observation.step
    if key not in _ALLOWED.get(step, frozenset()):
        note = "; ENT there can force the calibration" if step == _STEP.ERROR_DISPLAY else ""
        return f"{name} is not sent on {_step_name(step)}{note}"
    flags = _any_flag(observation)
    if step == _STEP.NONE and flags:
        channels = ", ".join(c.value for c in sorted(flags, key=lambda c: c.number))
        return f"a calibration flag is set on {channels}"
    if key is not KeyCode.ENT:
        return None
    return _ent_refusal(observation, cursor=cursor, calibrate=calibrate)


def _ent_refusal(
    observation: PanelObservation, *, cursor: ChannelId | None, calibrate: bool
) -> str | None:
    """Why ENT may not be sent on a step that allows it; ``None`` if it may."""
    if observation.step in _WAIT_STEPS:
        return None if calibrate else "ENT on the wait step calibrates; only calibrate() sends it"
    if cursor is None:
        return "ENT on channel selection needs the channel the cursor must be on"
    shown = observation.display.cursor_channel
    if shown != cursor:
        return (
            f"ENT with the cursor on {shown.value if shown else 'no channel'}, not {cursor.value}"
        )
    return None


async def _write_key(client: ProtocolClient, key: KeyCode, *, deadline: Deadline) -> bool:
    """Write ``key`` to 42001 once, with FC06; whether its reply came.

    Only :class:`RemoteCalibration` calls it, after reading the panel and
    checking the key against the step (:func:`key_refusal`). A lost reply
    leaves it to the reads that follow to say whether the key was taken. The
    write is never retried.

    Raises:
        FujiValidationError: ``key`` is not a calibration key; nothing was sent.
        FujiModbusError: the analyzer refused it with an exception reply.
        FujiWriteOutcomeUnknownError: the port failed while it waited for the reply.
        FujiError: it could not be sent; nothing was.
    """
    if int(key) not in CALIBRATION_KEYS:
        msg = f"0x{int(key):02X} is not one of the calibration keys; nothing was sent"
        raise FujiValidationError(
            msg, context=ErrorContext(register_address=KEY_SIMULATION_ADDRESS)
        )
    try:
        await client.write_register(
            KEY_SIMULATION_ADDRESS, int(key), deadline=deadline, command=f"key {_name(key)}"
        )
    except FujiWriteOutcomeUnknownError as exc:
        if exc.context.extra.get("failure") == FailureKind.CONNECTION.value:
            raise
        return False
    return True


@dataclass(frozen=True, slots=True)
class KeyPress:
    """One key written to 42001, and what the reads after it showed."""

    key: KeyCode
    step: ManualCalibrationStep | int
    """The step it was sent on."""
    cursor: ChannelId | None
    """Where the cursor was when it was sent."""
    sent_at: datetime
    acknowledged: bool
    """Whether the analyzer's reply came."""
    taken: bool
    """Whether the reads showed the panel act on it."""
    after_s: float | None
    """Seconds from the write to the read that showed it taken."""
    reads: int
    """Reads made after it."""
    purpose: str = ""
    """Why it was sent: the run, or the cleanup."""

    @property
    def name(self) -> str:
        """The key's name."""
        return _name(self.key)


# --- What the operator says is flowing --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationGas:
    """The gas the operator says is at the inlet, for one channel (design §13.1 #89).

    ``value`` is in ``unit``, which must be the channel's unit on the range
    calibrated; for a zero gas of 0 the unit may be left out. A unit given as
    text is kept as a :class:`~fujilib.registry.units.Unit`. ``label`` is
    kept in the record, e.g. ``"N2, cylinder 1234"`` or ``"20.95 % O2 in N2"``.
    """

    value: float
    unit: Unit | str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        """Refuse a value that is not a finite number.

        Raises:
            FujiValidationError: the value is not a finite number, or a gas
                other than 0 has no unit.
        """
        value = cast("object", self.value)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            msg = f"a calibration gas must be a finite number, got {value!r}"
            raise FujiValidationError(msg)
        if self.unit is None:
            if value != 0:
                msg = f"a calibration gas of {value:g} needs its unit, e.g. 'vol%' or 'ppm'"
                raise FujiValidationError(msg)
            return
        unit = coerce_unit(self.unit)
        if unit is Unit.UNKNOWN:
            msg = f"{self.unit!r} is not a unit of the ZP series: 'vol%', 'ppm', 'mg/m3' or 'g/m3'"
            raise FujiValidationError(msg)
        object.__setattr__(self, "unit", unit)


def _gases(
    gas: CalibrationGas | Mapping[ChannelId | str, CalibrationGas],
    channels: Sequence[ChannelId],
) -> Mapping[ChannelId, CalibrationGas]:
    """One gas per channel: a single gas stands for every channel."""
    if isinstance(gas, CalibrationGas):
        return MappingProxyType(dict.fromkeys(channels, gas))
    named: dict[ChannelId, CalibrationGas] = {}
    items = cast("Mapping[ChannelId | str, object]", gas)
    for channel, value in items.items():
        if not isinstance(value, CalibrationGas):
            msg = f"the gas for {channel} must be a CalibrationGas, got {value!r}"
            raise FujiValidationError(msg)
        named[coerce_channel(channel)] = value
    missing = [c.value for c in channels if c not in named]
    if missing:
        msg = f"no calibration gas named for {', '.join(missing)}, which the plan calibrates"
        raise FujiValidationError(msg)
    extra = [c.value for c in named if c not in channels]
    if extra:
        msg = f"a calibration gas named for {', '.join(extra)}, which the plan does not calibrate"
        raise FujiValidationError(msg)
    return MappingProxyType(named)


# --- Results -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WaitSample:
    """One read on the wait step: what the steadiness rule saw."""

    at: datetime
    readings: Mapping[ChannelId, float | None]
    counts: tuple[int, ...] | None
    """The detectors' raw A/D counts (A/D Nos. 0-4), when read."""


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """How the panel was returned to measurement."""

    clean: bool
    """The measurement screen, no step and no calibration flag at the end."""
    actions: tuple[str, ...]
    flags_left: tuple[ChannelId, ...] = ()
    """Channels whose calibration flag was still set at the end."""
    error: str | None = None
    """Why the panel could not be read or keyed, when it could not."""


class RunState(StrEnum):
    """Where a remote calibration is."""

    NEW = "new"
    WAITING = "waiting"
    """On the wait step, the channel selected: the gas settles."""
    ENDED = "ended"
    """The pass ended: calibrated, cancelled, or stopped."""
    CLOSED = "closed"
    """The block was left and the cleanup ran."""


@dataclass(frozen=True, slots=True)
class RemoteCalibrationResult:
    """A manual zero or span driven from the host, and how it ended."""

    plan: ManualCalibrationPlan
    gases: Mapping[ChannelId, CalibrationGas]
    rule: SteadinessRule
    event: ManualCalibrationEvent | None
    """The pass as the tracker saw it; ``None`` when no key opened one."""
    steadiness: SteadinessVerdict | None
    """The last verdict on the wait step."""
    calibrating_key_sent: bool
    """Whether the ENT that calibrates was sent."""
    keys: tuple[KeyPress, ...]
    cleanup: CleanupReport
    samples: tuple[WaitSample, ...]
    started_at: datetime
    ended_at: datetime
    error: str | None = None
    """What stopped the run, when something did."""

    @property
    def outcome(self) -> ManualCalibrationOutcome | None:
        """The event's outcome; ``None`` when no pass was opened."""
        return self.event.outcome if self.event is not None else None

    def as_record(
        self,
        *,
        info: DeviceInfo | None = None,
        port: str | None = None,
        address: int | None = None,
        operator: str | None = None,
        notes: str | None = None,
    ) -> dict[str, object]:
        """The run as a ``fujilib-calibration/1`` document (design §13.1 #75, #90)."""
        if self.event is not None:
            record = panel.calibration_record(
                self.event, info=info, port=port, address=address, source="remote"
            )
        else:
            record = panel.calibration_record_header(
                info=info, port=port, address=address, source="remote"
            )
            record |= {"kind": self.plan.kind.value, "outcome": None}
        record |= {
            "plan": {
                "channel": self.plan.channel.value,
                "targets": [
                    {
                        "channel": t.channel.value,
                        "ranges": list(t.ranges),
                        "established": t.established,
                        "zero_gas": list(t.zero_gas),
                        "span_gas": list(t.span_gas),
                        "units": list(t.units),
                    }
                    for t in self.plan.targets
                ],
                "notes": list(self.plan.notes),
            },
            "named_gas": {
                c.value: {
                    "value": g.value,
                    "unit": str(g.unit) if g.unit is not None else None,
                    "label": g.label,
                }
                for c, g in self.gases.items()
            },
            "steadiness": _verdict_record(self.rule, self.steadiness),
            "calibrating_key_sent": self.calibrating_key_sent,
            "wait_series": [
                {
                    "at": s.at.isoformat(),
                    "readings": {c.value: v for c, v in s.readings.items()},
                    "counts": list(s.counts) if s.counts is not None else None,
                }
                for s in self.samples
            ],
            "keys": [
                {
                    "key": k.name,
                    "step": int(k.step),
                    "cursor": k.cursor.value if k.cursor is not None else None,
                    "sent_at": k.sent_at.isoformat(),
                    "acknowledged": k.acknowledged,
                    "taken": k.taken,
                    "after_s": k.after_s,
                    "purpose": k.purpose,
                }
                for k in self.keys
            ],
            "cleanup": {
                "clean": self.cleanup.clean,
                "actions": list(self.cleanup.actions),
                "flags_left": [c.value for c in self.cleanup.flags_left],
                "error": self.cleanup.error,
            },
            "run_started_at": self.started_at.isoformat(),
            "run_ended_at": self.ended_at.isoformat(),
            "error": self.error,
            "operator": operator,
            "notes": notes,
        }
        return record


def _verdict_record(rule: SteadinessRule, verdict: SteadinessVerdict | None) -> dict[str, object]:
    return {
        "rule": {
            "window_s": rule.window_s,
            "response_factor": rule.response_factor,
            "band_percent_fs": rule.band_percent_fs,
            "tolerance_percent_fs": rule.tolerance_percent_fs,
            "timeout_s": rule.timeout_s,
            "max_gap_s": rule.max_gap_s,
        },
        "steady": verdict.steady if verdict is not None else None,
        "elapsed_s": verdict.elapsed_s if verdict is not None else None,
        "reads": verdict.reads if verdict is not None else 0,
        "channels": {
            c.value: {
                "steady": s.steady,
                "window_s": s.window_s,
                "covered_s": s.covered_s,
                "last": s.last,
                "mean": s.mean,
                "spread_percent_fs": s.spread_percent_fs,
                "offset_percent_fs": s.offset_percent_fs,
                "reason": s.reason,
            }
            for c, s in (verdict.channels.items() if verdict is not None else ())
        },
    }


# --- The run ------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Timing:
    interval: float
    key_timeout: float
    run_timeout: float
    cleanup_timeout: float
    flag_wait: float


@dataclass(slots=True)
class _Checked:
    """What the reads before a key that matters showed."""

    observation: PanelObservation
    status: StatusRead
    settings: Mapping[str, RegisterValue] = field(default_factory=lambda: MappingProxyType({}))


class RemoteCalibration:
    """A manual zero or span driven from the host (see the module docstring).

    Made by :meth:`Analyzer.manual_calibration
    <fujilib.devices.analyzer.Analyzer.manual_calibration>`; use it as an async
    context manager, once.
    """

    def __init__(
        self,
        session: Session,
        plan: ManualCalibrationPlan,
        *,
        gas: CalibrationGas | Mapping[ChannelId | str, CalibrationGas],
        confirm: bool,
        rule: SteadinessRule | None = None,
        adc: bool = False,
        interval: float = 0.5,
        key_timeout: float = 2.0,
        run_timeout: float = 30.0,
        cleanup_timeout: float = 30.0,
    ) -> None:
        """Prepare the run; nothing is sent until the block is entered.

        Raises:
            FujiValidationError: a gas is missing or malformed, or a time is
                not a positive number of seconds.
        """
        for name, value in (
            ("interval", interval),
            ("key_timeout", key_timeout),
            ("run_timeout", run_timeout),
            ("cleanup_timeout", cleanup_timeout),
        ):
            _check_seconds(name, value)
        self._session = session
        self._plan = plan
        self._established = tuple(t.channel for t in plan.targets if t.established)
        if not self._established:
            channels = ", ".join(c.value for c in plan.channels)
            msg = (
                f"none of the channels the plan calibrates ({channels}) is established, so "
                "none can be watched"
            )
            raise FujiValidationError(msg)
        self._gases = _gases(gas, self._established)
        self._confirmed = confirm
        self._rule = rule if rule is not None else SteadinessRule()
        self._adc = adc
        self._timing = _Timing(interval, key_timeout, run_timeout, cleanup_timeout, flag_wait=1.0)
        self._tracker = ManualCalibrationTracker()
        self._state = RunState.NEW
        self._keys: list[KeyPress] = []
        self._samples: list[WaitSample] = []
        self._event: ManualCalibrationEvent | None = None
        self._verdict: SteadinessVerdict | None = None
        self._judge: SteadinessJudge | None = None
        self._last: PanelObservation | None = None
        self._status: StatusRead | None = None
        self._calibrated = False
        self._entered = False
        self._started_at = datetime.now(UTC)
        self._result: RemoteCalibrationResult | None = None
        self._error: str | None = None

    # --- What is known -------------------------------------------------------------------

    @property
    def plan(self) -> ManualCalibrationPlan:
        """What the run calibrates."""
        return self._plan

    @property
    def gases(self) -> Mapping[ChannelId, CalibrationGas]:
        """The gas named for each established channel the plan calibrates."""
        return self._gases

    @property
    def rule(self) -> SteadinessRule:
        """The steadiness rule applied."""
        return self._rule

    @property
    def state(self) -> RunState:
        """Where the run is."""
        return self._state

    @property
    def observation(self) -> PanelObservation | None:
        """The last read of the panel."""
        return self._last

    @property
    def steadiness(self) -> SteadinessVerdict | None:
        """The last verdict on the wait step."""
        return self._verdict

    @property
    def keys(self) -> tuple[KeyPress, ...]:
        """The keys written so far."""
        return tuple(self._keys)

    @property
    def event(self) -> ManualCalibrationEvent | None:
        """The pass, once it has ended."""
        return self._event

    @property
    def result(self) -> RemoteCalibrationResult | None:
        """The whole run, once the block has been left."""
        return self._result

    # --- Entering and leaving ------------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Check everything, then press ZERO or SPAN, move the cursor and select the channel.

        Raises:
            FujiConfirmationRequiredError: ``confirm`` was not ``True``; nothing was sent.
            FujiAnalyzerStateError: the panel, a setting or the plan forbids it,
                or a key was not taken; the panel was then returned to
                measurement.
            FujiCapabilityError: A/D values were asked for and the analyzer has none.
            FujiError: a read or a key failed; the panel was then returned to measurement.
        """
        if self._entered:
            msg = "a remote calibration is used once; make another with manual_calibration()"
            raise FujiValidationError(msg)
        self._entered = True
        self._started_at = datetime.now(UTC)
        requires = Capability.ADC_VALUES if self._adc else Capability.NONE
        session = self._session
        session.gate(
            _OPERATION,
            tier=SafetyTier.STATEFUL,
            confirm=self._confirmed,
            requires=requires,
            subject="a remote manual calibration",
        )
        session.claim_panel(_OPERATION)
        try:
            checked = await self._preflight()
            self._judge = self._make_judge(checked.settings)
            await self._open()
            await self._navigate()
            await self._select()
        except BaseException as exc:
            self._error = str(exc) or type(exc).__name__
            with anyio.CancelScope(shield=True):
                await self._close(exc)
            raise
        self._state = RunState.WAITING
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Return the panel to measurement for the step it is on (see the module docstring).

        Raises:
            FujiAnalyzerStateError: a calibration flag is still set on the
                measurement screen, or the panel could not be returned to it.
        """
        if exc is not None and self._error is None:
            self._error = str(exc) or type(exc).__name__
        with anyio.CancelScope(shield=True):
            await self._close(exc)

    async def _close(self, exc: BaseException | None) -> None:
        report = CleanupReport(
            clean=False, actions=(), error="the cleanup was interrupted before it finished"
        )
        try:
            report = await self._clean_up()
        finally:
            self._session.release_panel()
            self._state = RunState.CLOSED
            self._result = self._build(report)
        if exc is not None and not report.clean:
            exc.add_note(f"The front panel was not left clean: {_cleanup_summary(report)}")
        replaceable = exc is None or isinstance(exc, Exception)
        if not replaceable:
            return
        if report.flags_left:
            channels = ", ".join(c.value for c in report.flags_left)
            msg = (
                f"the calibration flag of {channels} is still set on the measurement screen "
                "after the cleanup; the analyzer still counts it as being calibrated. Recover "
                f"at the panel: {self._plan.kind.value.upper()}, the channel, ENT once (the wait "
                "step), then ESC (protocol findings §18.4)"
            )
            raise FujiAnalyzerStateError(
                msg, context=ErrorContext(command_name=_OPERATION, extra={"channels": channels})
            ) from exc
        if not report.clean and exc is None:
            msg = (
                "the front panel could not be returned to the measurement screen: "
                f"{_cleanup_summary(report)}; check the panel"
            )
            raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=_OPERATION))

    def _build(self, report: CleanupReport) -> RemoteCalibrationResult:
        return RemoteCalibrationResult(
            plan=self._plan,
            gases=self._gases,
            rule=self._rule,
            event=self._event,
            steadiness=self._verdict,
            calibrating_key_sent=self._calibrated,
            keys=tuple(self._keys),
            cleanup=report,
            samples=tuple(self._samples),
            started_at=self._started_at,
            ended_at=datetime.now(UTC),
            error=self._error,
        )

    def _with_gases(self, event: ManualCalibrationEvent) -> ManualCalibrationEvent:
        """``event`` with each channel's calibration-gas setting, from the plan."""
        gases: dict[ChannelId, float | None] = {}
        for target in self._plan.targets:
            rng = event.ranges.get(target.channel)
            if rng is None or rng not in target.ranges:
                continue
            index = target.ranges.index(rng)
            settings = (
                target.zero_gas
                if self._plan.kind is ManualCalibrationKind.ZERO
                else target.span_gas
            )
            gases[target.channel] = settings[index]
        return replace(event, gases=MappingProxyType(gases))

    # --- Reads ---------------------------------------------------------------------------

    async def _observe(
        self, client: ProtocolClient, deadline: Deadline, *, adc: bool = False
    ) -> tuple[PanelObservation, StatusRead]:
        """One read of the panel: the poll's two blocks, and the A/D block with ``adc``."""
        session = self._session
        poll = await reads.read_poll(client, deadline=deadline)
        frame = session.learn_poll(poll)
        status = poll.status()
        values = None
        if adc:
            values = await session.read_capability(
                Capability.ADC_VALUES,
                _OPERATION,
                lambda: reads.read_adc(client, deadline=deadline),
            )
        display = status.analyzer.display
        assert display is not None  # noqa: S101 - a poll with detail always decodes it
        self._status = status
        observation = PanelObservation(
            at=status.timings[-1].received_at,
            display=display,
            channels=status.channels,
            calibration_error=status.analyzer.calibration_error,
            readings=MappingProxyType({r.channel: r for r in frame.readings}),
            adc=values,
        )
        self._take(observation)
        return observation, status

    def _take(self, observation: PanelObservation) -> None:
        self._last = observation
        event = self._tracker.feed(observation)
        if event is not None:
            self._event = self._with_gases(event)
            self._state = RunState.ENDED

    async def _read_settings(
        self, client: ProtocolClient, deadline: Deadline
    ) -> tuple[Mapping[str, RegisterValue], tuple[RangeInfo, ...]]:
        session = self._session
        ranges = await session.ensure_ranges(client, deadline)
        settings = await reads.read_registers(
            client, _RUN_SETTINGS, ranges=ranges, deadline=deadline
        )
        return settings, ranges

    # --- The checks ----------------------------------------------------------------------

    async def _preflight(self) -> _Checked:
        """Everything that must hold before the first key; nothing is sent."""
        operation = f"{_OPERATION} (checks)"

        async def body(client: ProtocolClient, deadline: Deadline) -> _Checked:
            observation, status = await self._observe(client, deadline)
            settings, ranges = await self._read_settings(client, deadline)
            reasons = self._settings_refusals(settings, ranges)
            reasons += _panel_refusals(observation, status)
            if reasons:
                msg = f"a remote manual calibration refused, nothing was sent: {'; '.join(reasons)}"
                raise FujiAnalyzerStateError(
                    msg,
                    context=ErrorContext(
                        command_name=_OPERATION, extra={"reasons": tuple(reasons)}
                    ),
                )
            check_healthy(status, "a remote manual calibration")
            return _Checked(observation, status, settings)

        return await self._session.run(operation, body)

    def _settings_refusals(
        self, settings: Mapping[str, RegisterValue], ranges: Sequence[RangeInfo]
    ) -> list[str]:
        """What the settings forbid: key lock, output hold, a changed or widened plan, the gases."""
        reasons: list[str] = []
        if settings["key_lock"].raw:
            reasons.append(
                "key lock is on: it would swallow the keys, and the analyzer would then not "
                "answer for about two seconds (protocol findings §18.6); switch it off at the panel"
            )
        if settings["output_hold.enabled"].raw:
            reasons.append(
                "output hold is on: the readings would be held during the calibration, so the "
                "gas could not be judged steady (design §13.1 #86)"
            )
        try:
            again = panel.plan_manual_calibration(
                settings,
                self._plan.kind,
                self._plan.channel,
                ranges=ranges,
                established=[c.channel for c in self._session.channels],
            )
        except FujiError as exc:
            reasons.append(f"the plan cannot be made again: {exc}")
            return reasons
        if again.targets != self._plan.targets:
            reasons.append(
                "the plan has changed since it was made: its channels, ranges or calibration "
                "gases read otherwise now; make the plan again"
            )
            return reasons
        widened = [t.channel.value for t in self._plan.targets if t.widened]
        if widened:
            reasons.append(
                f"{', '.join(widened)} is set to calibrate both ranges, which the bench analyzer "
                "has not shown (design §13.1 #88); set its calibration range to 'current'"
            )
        reasons += self._gas_refusals(ranges)
        return reasons

    def _gas_refusals(self, ranges: Sequence[RangeInfo]) -> list[str]:
        """The named gas must be each channel's calibration-gas setting (design §13.1 #89)."""
        reasons: list[str] = []
        tables = {r.channel: r for r in ranges}
        zero = self._plan.kind is ManualCalibrationKind.ZERO
        for target in self._plan.targets:
            if not target.established:
                continue
            named = self._gases[target.channel]
            for index, rng in enumerate(target.ranges):
                table = tables.get(target.channel)
                if table is None:
                    reasons.append(f"{target.channel.value}'s range table was not read")
                    continue
                unit, _, decimals = table.of(rng)
                setting = (target.zero_gas if zero else target.span_gas)[index]
                what = f"{target.channel.value} range {rng}"
                if named.unit is not None and (given := coerce_unit(named.unit)) is not unit:
                    reasons.append(f"{what} measures in {unit.value}, not {given.value}")
                    continue
                if setting is None:
                    reasons.append(f"{what}: its calibration-gas setting does not decode")
                    continue
                if round(named.value, decimals) != round(setting, decimals):
                    kind = "zero" if zero else "span"
                    reasons.append(
                        f"{what}: the gas named is {named.value:g} {unit.value}, but its "
                        f"{kind}-gas setting is {setting:g}; the analyzer calibrates against the "
                        "setting, so change the setting first (set_calibration_gas) or name the "
                        "gas flowing"
                    )
        return reasons

    def _make_judge(self, settings: Mapping[str, RegisterValue]) -> SteadinessJudge:
        session = self._session
        tables = {r.channel: r for r in (session.ranges or ())}
        targets: dict[ChannelId, SteadinessTarget] = {}
        times = {
            name: int(settings[name].raw)
            for name in ("response_time.o2", *(f"response_time.ndir{k}" for k in range(1, 5)))
        }
        for target in self._plan.targets:
            if not target.established:
                continue
            unit, full_scale, _ = tables[target.channel].of(target.ranges[0])
            targets[target.channel] = SteadinessTarget(
                gas=self._gases[target.channel].value,
                full_scale=full_scale,
                unit=unit.value,
                response_time_s=_response_time(target.channel, session.asserted, times),
            )
        return SteadinessJudge(self._rule, targets)

    # --- The keys --------------------------------------------------------------------------

    async def _key(
        self,
        key: KeyCode,
        *,
        done: Callable[[PanelObservation, PanelObservation], bool | str],
        purpose: str,
        cursor: ChannelId | None = None,
        calibrate: bool = False,
        before_key: Callable[
            [ProtocolClient, Deadline, PanelObservation], Awaitable[PanelObservation]
        ]
        | None = None,
        budget: float | None = None,
        tier: SafetyTier = SafetyTier.STATEFUL,
        confirm: bool | None = None,
        adc: bool = False,
        step: ManualCalibrationStep | None = None,
        timeout: float | None = None,
    ) -> PanelObservation:
        """Send ``key`` as one locked operation (see the module docstring); the read that took it.

        ``step`` is the step the key must be sent on, when it matters which.
        ``done(before, after)`` says whether a read on the measurement screen
        shows the key taken (``True``), not yet (``False``), or something
        unexpected (a reason); any other screen is unexpected. ``timeout``
        bounds the operation, the wait for the port included.

        Once the key may have reached the panel it is recorded, before it is
        written, so the cleanup reads the panel after it however this ends;
        and unless the reads show it taken, the run ends there: no later key
        but the cleanup's is sent.

        Raises:
            FujiAnalyzerStateError: the panel does not allow the key (nothing
                sent), did not take it, or did something unexpected.
        """
        name = _name(key)
        operation = f"{_OPERATION} key {name}"
        budget_s = self._timing.key_timeout if budget is None else budget

        async def body(client: ProtocolClient, deadline: Deadline) -> PanelObservation:
            before, _ = await self._observe(client, deadline, adc=adc)
            refusal = key_refusal(key, before, cursor=cursor, calibrate=calibrate)
            if refusal is None and step is not None and before.step != step:
                refusal = f"it goes on {_step_name(step)}, and the panel is on {_describe(before)}"
            if refusal is not None:
                msg = f"{name} refused, nothing was sent: {refusal}"
                raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=operation))
            if before_key is not None:
                before = await before_key(client, deadline, before)
            return await self._send(
                client,
                deadline,
                key,
                before,
                done=done,
                budget=budget_s,
                purpose=purpose,
                operation=operation,
            )

        return await self._session.run(
            operation,
            body,
            timeout=timeout,
            tier=tier,
            confirm=self._confirmed if confirm is None else confirm,
        )

    async def _send(
        self,
        client: ProtocolClient,
        deadline: Deadline,
        key: KeyCode,
        before: PanelObservation,
        *,
        done: Callable[[PanelObservation, PanelObservation], bool | str],
        budget: float,
        purpose: str,
        operation: str,
    ) -> PanelObservation:
        """Write ``key`` and confirm it (see :meth:`_key`)."""
        name = _name(key)
        index = len(self._keys)
        press = KeyPress(
            key=key,
            step=before.step,
            cursor=before.display.cursor_channel,
            sent_at=datetime.now(UTC),
            acknowledged=False,
            taken=False,
            after_s=None,
            reads=0,
            purpose=purpose,
        )
        self._keys.append(press)
        calibrating = purpose == _CALIBRATE
        self._calibrated = self._calibrated or calibrating
        started = anyio.current_time()
        try:
            acknowledged = await _write_key(client, key, deadline=deadline)
        except FujiModbusError:
            # A definite refusal: the analyzer did not act on the key.
            del self._keys[index]
            self._calibrated = self._calibrated and not calibrating
            self._state = RunState.ENDED
            raise
        except BaseException:
            self._state = RunState.ENDED
            raise
        try:
            after, taken, reads_ = await self._confirm(client, before, done, budget, operation)
        except BaseException:
            self._keys[index] = replace(press, acknowledged=acknowledged)
            self._state = RunState.ENDED
            raise
        self._keys[index] = replace(
            press,
            acknowledged=acknowledged,
            taken=taken is True,
            after_s=anyio.current_time() - started if taken is True else None,
            reads=reads_,
        )
        if taken is True:
            assert after is not None  # noqa: S101 - taken rests on a read
            return after
        self._state = RunState.ENDED
        if isinstance(taken, str):
            msg = f"{name}: {taken}"
        else:
            sent = "was acknowledged" if acknowledged else "went unanswered"
            msg = (
                f"{name} {sent}, but the panel did not show it taken within {budget:g} s: "
                "key lock, the backlight or a key pressed at the panel can do that"
            )
            if after is not None:
                msg += f" (the panel shows {_describe(after)})"
        raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=operation))

    async def _confirm(
        self,
        client: ProtocolClient,
        before: PanelObservation,
        done: Callable[[PanelObservation, PanelObservation], bool | str],
        budget: float,
        operation: str,
    ) -> tuple[PanelObservation | None, bool | str, int]:
        """Read until ``done`` says taken or something unexpected, or ``budget`` runs out.

        Shielded, with its own deadline, as a read-back is: once a key is sent,
        what it did is established even if the caller gives up. A read that
        fails is tried again within the budget: the analyzer does not answer
        for a moment while it stores a calibration (protocol findings §14.3).
        """
        deadline = Deadline.after(budget, operation=f"{operation} (confirm)")
        last: PanelObservation | None = None
        count = 0
        state: bool | str = False
        with anyio.CancelScope(shield=True):
            while deadline.remaining() > 0:
                try:
                    last, _ = await self._observe(client, deadline)
                except FujiConnectionError:
                    raise
                except FujiError:
                    await anyio.sleep(min(_CONFIRM_POLL_S, max(0.0, deadline.remaining())))
                    continue
                count += 1
                screen = last.display.screen
                if screen != DisplayScreen.MEASUREMENT:
                    state = f"the panel shows {_screen_name(screen)}"
                    break
                state = done(before, last)
                if state is not False:
                    break
                await anyio.sleep(min(_CONFIRM_POLL_S, max(0.0, deadline.remaining())))
        return last, state, count

    async def _open(self) -> None:
        kind = self._plan.kind
        wanted = _SELECT[kind]

        def done(_before: PanelObservation, after: PanelObservation) -> bool | str:
            if after.step == wanted:
                return True
            if after.step == _STEP.NONE:
                return False
            return f"it opened {_step_name(after.step)}, not {_step_name(wanted)}"

        await self._key(_OPEN[kind], done=done, purpose="open channel selection")

    @property
    def _target(self) -> ChannelId:
        """Where the cursor must be: the channel, or the first "at once" channel of a zero."""
        if len(self._plan.targets) > 1:
            return min(self._established, key=lambda c: c.number)
        return self._plan.channel

    async def _navigate(self) -> None:
        """Move the cursor down, one confirmed key at a time, to :attr:`_target`."""
        target = self._target
        wanted = _SELECT[self._plan.kind]
        seen: set[ChannelId | None] = set()

        def done(before: PanelObservation, after: PanelObservation) -> bool | str:
            if after.step != wanted:
                return f"the panel left channel selection: {_describe(after)}"
            return after.display.cursor_channel != before.display.cursor_channel

        # Each DOWN moves the cursor on, so it comes round to a position already
        # seen once it has passed them all.
        while True:
            last = self._last
            assert last is not None  # noqa: S101 - the key before read the panel
            here = last.display.cursor_channel
            if here == target:
                return
            if here in seen:
                break
            seen.add(here)
            await self._key(
                KeyCode.DOWN, done=done, purpose=f"move the cursor to {target.value}", step=wanted
            )
        msg = (
            f"the cursor did not reach {target.value}: the panel does not offer it on "
            f"{self._plan.kind.value} channel selection"
        )
        raise FujiAnalyzerStateError(
            msg, context=ErrorContext(command_name=_OPERATION, channel=target.value)
        )

    async def _select(self) -> None:
        kind = self._plan.kind
        wanted = _WAIT[kind]
        expected = frozenset(self._established)

        def done(_before: PanelObservation, after: PanelObservation) -> bool | str:
            if after.step == _SELECT[kind]:
                return False
            if after.step != wanted:
                return f"it went to {_step_name(after.step)}, not {_step_name(wanted)}"
            flags = _flagged(after, kind)
            if flags - expected:
                extra = ", ".join(c.value for c in sorted(flags - expected, key=lambda c: c.number))
                return f"it set the calibration flag of {extra}, which the plan does not calibrate"
            return flags == expected

        await self._key(
            KeyCode.ENT,
            done=done,
            cursor=self._target,
            purpose="select the channel",
            step=_SELECT[kind],
        )

    # --- Inside the block ------------------------------------------------------------------

    def _require_waiting(self, what: str) -> None:
        if self._state is not RunState.WAITING:
            msg = f"{what}: the run is {self._state.value}, not on the wait step"
            raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=_OPERATION))

    def _check_waiting(self, observation: PanelObservation) -> None:
        """Stop the run unless the panel is still on the plan's wait step, its flags set."""
        kind = self._plan.kind
        if observation.step == _WAIT[kind] and _flagged(observation, kind) == frozenset(
            self._established
        ):
            return
        self._state = RunState.ENDED
        msg = f"the panel left the wait step: {_describe(observation)}"
        raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=_OPERATION))

    def _unit_refusals(self, observation: PanelObservation) -> list[str]:
        """Channels whose reading is not in the unit of the range calibrated."""
        judge = self._judge
        assert judge is not None  # noqa: S101 - made before the first key
        reasons: list[str] = []
        for channel, target in judge.targets.items():
            reading = observation.readings.get(channel)
            if reading is not None and reading.unit.value != target.unit:
                reasons.append(
                    f"{channel.value} reads in {reading.unit.value}, not {target.unit}, the unit "
                    "of the range calibrated"
                )
        return reasons

    def _judge_read(self, observation: PanelObservation) -> SteadinessVerdict:
        judge = self._judge
        assert judge is not None  # noqa: S101 - made before the first key
        readings = {
            c: (r.value if (r := observation.readings.get(c)) is not None else None)
            for c in judge.targets
        }
        counts = tuple(observation.adc.inputs) if observation.adc is not None else None
        self._samples.append(WaitSample(observation.at, MappingProxyType(readings), counts))
        verdict = judge.feed(anyio.current_time(), readings)
        self._verdict = verdict
        return verdict

    async def read(self, *, timeout: float | None = None) -> SteadinessVerdict:
        """Read the panel once on the wait step, and judge the gas.

        Raises:
            FujiAnalyzerStateError: the run is not on the wait step, or the
                panel has left it.
            FujiError: the read failed.
        """
        self._require_waiting("read")
        operation = f"{_OPERATION} (wait)"

        async def body(client: ProtocolClient, deadline: Deadline) -> SteadinessVerdict:
            observation, _ = await self._observe(client, deadline, adc=self._adc)
            self._check_waiting(observation)
            return self._judge_read(observation)

        return await self._session.run(operation, body, timeout=timeout)

    async def wait_steady(
        self,
        *,
        timeout: float | None = None,
        progress: Callable[[SteadinessVerdict], object] | None = None,
    ) -> SteadinessVerdict:
        """Read every ``interval`` until the gas is steady on every channel; the verdict.

        The port is free between reads. ``timeout`` defaults to the rule's
        ``timeout_s``. ``progress`` is called with each verdict, on the event
        loop.

        Raises:
            FujiTimeoutError: not steady within ``timeout``; its context gives why.
            FujiAnalyzerStateError: the run is not on the wait step, or the panel left it.
            FujiValidationError: ``timeout`` is not a positive number of seconds.
            FujiError: a read failed.
        """
        self._require_waiting("wait_steady")
        limit = _check_seconds("timeout", self._rule.timeout_s if timeout is None else timeout)
        deadline = Deadline.after(limit, operation=f"{_OPERATION} wait_steady")
        while True:
            started = anyio.current_time()
            verdict = await self.read()
            if progress is not None:
                progress(verdict)
            if verdict.steady:
                return verdict
            if deadline.remaining() <= 0:
                msg = f"the gas was not steady within {limit:g} s: " + "; ".join(verdict.reasons)
                raise FujiTimeoutError(
                    msg,
                    context=ErrorContext(
                        command_name=f"{_OPERATION} wait_steady",
                        elapsed_s=deadline.elapsed(),
                        extra={"reasons": verdict.reasons},
                    ),
                )
            pause = self._timing.interval - (anyio.current_time() - started)
            await anyio.sleep(max(0.0, min(pause, deadline.remaining())))

    async def calibrate(self, *, confirm: bool = False) -> ManualCalibrationEvent:
        """Send the ENT that calibrates, and follow the calibration to its end. DANGEROUS.

        Everything is checked again first, in the same operation as the key:
        the panel on the wait step with the plan's flags, the gas still steady
        on every channel with this read, the plan, key lock, output hold and
        the instrument errors. The calibration is then followed until the
        panel is back on measurement; on the error display it sends ESC,
        never ENT.

        Raises:
            FujiConfirmationRequiredError: ``confirm`` is not ``True``; nothing was sent.
            FujiAnalyzerStateError: not on the wait step, not steady, or a
                check failed; nothing was sent. Or the key was not taken.
            FujiError: a read failed.
        """
        operation = f"{_OPERATION} calibrate"
        session = self._session
        session.gate(
            operation,
            tier=SafetyTier.DANGEROUS,
            confirm=confirm,
            subject="the key that starts a manual calibration",
        )
        self._require_waiting("calibrate")
        verdict = self._verdict
        if verdict is None or not verdict.steady:
            why = (
                "; ".join(verdict.reasons)
                if verdict is not None
                else "no read on the wait step yet"
            )
            msg = (
                f"calibrate refused, nothing was sent: the gas is not steady ({why}); "
                "wait_steady() first"
            )
            raise FujiAnalyzerStateError(msg, context=ErrorContext(command_name=operation))

        async def check(
            client: ProtocolClient, deadline: Deadline, before: PanelObservation
        ) -> PanelObservation:
            # The settings first, then the panel again, so that the panel is the last
            # thing read before the key.
            self._check_waiting(before)
            settings, ranges = await self._read_settings(client, deadline)
            reasons = self._settings_refusals(settings, ranges)
            latest, status = await self._observe(client, deadline, adc=self._adc)
            self._check_waiting(latest)
            again = self._judge_read(latest)
            if not again.steady:
                reasons.append("the gas is not steady with this read: " + "; ".join(again.reasons))
            reasons += _panel_refusals(latest, status, waiting=True)
            reasons += self._unit_refusals(latest)
            analyzer = status.analyzer
            if analyzer.instrument_error or analyzer.errors:
                codes = ", ".join(str(int(e)) for e in sorted(analyzer.errors)) or "unnumbered"
                reasons.append(f"the analyzer reports an instrument error ({codes})")
            if reasons:
                msg = f"calibrate refused, nothing was sent: {'; '.join(reasons)}"
                raise FujiAnalyzerStateError(
                    msg,
                    context=ErrorContext(command_name=operation, extra={"reasons": tuple(reasons)}),
                )
            return latest

        def took(_before: PanelObservation, after: PanelObservation) -> bool | str:
            # It ran, failed, or already finished: the tracker says which.
            step = after.step
            if step in _RUNNING_STEPS or step in {_STEP.ERROR_DISPLAY, _STEP.NONE}:
                return True
            if step == _WAIT[self._plan.kind]:
                return False
            return f"it went to {_step_name(step)}"

        await self._key(
            KeyCode.ENT,
            done=took,
            purpose=_CALIBRATE,
            calibrate=True,
            before_key=check,
            budget=self._timing.run_timeout,
            tier=SafetyTier.DANGEROUS,
            confirm=confirm,
            step=_WAIT[self._plan.kind],
        )
        with anyio.CancelScope(shield=True):
            await self._follow()
        event = self._event
        if event is None:
            self._state = RunState.ENDED
            msg = (
                f"the calibration did not end within {self._timing.run_timeout:g} s; the cleanup "
                "will wait for it"
            )
            raise FujiTimeoutError(msg, context=ErrorContext(command_name=operation))
        return event

    async def _follow(self) -> None:
        """Read until the pass ends; ESC on the error display (never ENT)."""
        deadline = Deadline.after(self._timing.run_timeout, operation=f"{_OPERATION} follow")
        escaped = False
        while self._event is None and deadline.remaining() > 0:
            observation = await self._read_quietly(deadline)
            if observation is None:
                break
            if observation.step == _STEP.ERROR_DISPLAY and not escaped:
                escaped = True
                await self._escape("leave the error display")
                continue
            if self._event is None:
                await anyio.sleep(min(_FOLLOW_POLL_S, max(0.0, deadline.remaining())))

    async def _read_quietly(self, deadline: Deadline) -> PanelObservation | None:
        """One read as its own operation, tried again within ``deadline``; ``None`` if none came."""
        while deadline.remaining() > 0:

            async def body(client: ProtocolClient, dl: Deadline) -> PanelObservation:
                observation, _ = await self._observe(client, dl)
                return observation

            try:
                return await self._session.run(
                    f"{_OPERATION} (read)", body, timeout=max(deadline.remaining(), 0.0)
                )
            except FujiConnectionError:
                raise
            except FujiError:
                await anyio.sleep(min(_CONFIRM_POLL_S, max(0.0, deadline.remaining())))
        return None

    async def _escape(self, purpose: str, *, timeout: float | None = None) -> PanelObservation:
        kind = self._plan.kind

        def done(before: PanelObservation, after: PanelObservation) -> bool | str:
            if after.step == before.step:
                return False
            if after.step != _STEP.NONE:
                return f"it went to {_step_name(after.step)}"
            return not (_flagged(after, kind) & frozenset(self._established))

        return await self._key(
            KeyCode.ESC, done=done, purpose=purpose, confirm=True, timeout=timeout
        )

    async def cancel(self) -> ManualCalibrationEvent | None:
        """Leave the wait step with ESC, which clears the flags; the pass's event.

        Raises:
            FujiAnalyzerStateError: the run is not on the wait step, or ESC was not taken.
        """
        self._require_waiting("cancel")
        await self._escape("cancel")
        self._state = RunState.ENDED
        return self._event

    # --- Cleanup ---------------------------------------------------------------------------

    async def _clean_up(self) -> CleanupReport:
        """Return the panel to measurement for the step it is on (see the module docstring)."""
        if not self._keys:
            return CleanupReport(clean=True, actions=())
        actions: list[str] = []
        sent: set[tuple[KeyCode | str, int]] = set()
        last = self._keys[-1]
        sent.add((last.key, int(last.step)))
        deadline = Deadline.after(self._timing.cleanup_timeout, operation=f"{_OPERATION} cleanup")
        error: str | None = None
        observation: PanelObservation | None = None
        try:
            # Each cleanup key is sent once, so the steps run out before the loop does.
            for _ in range(_CLEANUP_STEPS):  # pragma: no branch
                observation = await self._read_quietly(deadline)
                if observation is None:
                    error = "the panel could not be read"
                    break
                if not await self._clean_step(observation, sent, actions, deadline):
                    break
            observation = await self._settle_flags(deadline) or observation
        except FujiError as exc:
            error = str(exc)
        if observation is None:
            return CleanupReport(clean=False, actions=tuple(actions), error=error or "no read")
        flags = _any_flag(observation)
        on_measurement = observation.display.screen == DisplayScreen.MEASUREMENT
        clean = on_measurement and observation.step == _STEP.NONE and not flags
        left = (
            tuple(sorted(flags, key=lambda c: c.number))
            if on_measurement and observation.step == _STEP.NONE
            else ()
        )
        if not clean:
            _LOG.warning(
                "%s station %d: the remote calibration's cleanup did not end clean: %s",
                self._session.port,
                self._session.address,
                _describe(observation),
            )
        return CleanupReport(clean=clean, actions=tuple(actions), flags_left=left, error=error)

    async def _clean_step(
        self,
        observation: PanelObservation,
        sent: set[tuple[KeyCode | str, int]],
        actions: list[str],
        deadline: Deadline,
    ) -> bool:
        """One cleanup action for ``observation``; ``False`` when there is nothing more to do."""
        step = observation.step
        if observation.display.screen != DisplayScreen.MEASUREMENT or step not in _CLEANUP:
            return await self._clean_return(observation, sent, actions, deadline)
        if step == _STEP.NONE:
            return False
        if step in _RUNNING_STEPS:
            actions.append(f"waited for {_step_name(step)} to end")
            return await self._wait_running(deadline)
        if (KeyCode.ESC, int(step)) in sent:
            actions.append(f"ESC already sent on {_step_name(step)}; stopped")
            return False
        sent.add((KeyCode.ESC, int(step)))
        written = len(self._keys)
        try:
            await self._escape("cleanup", timeout=max(deadline.remaining(), 0.0))
        except FujiConnectionError:
            raise
        except FujiError as exc:
            actions.append(f"ESC on {_step_name(step)}: {exc}")
            if len(self._keys) == written:
                sent.discard((KeyCode.ESC, int(step)))  # never written: it may be tried again
        else:
            actions.append(f"ESC on {_step_name(step)}")
        return True

    async def _wait_running(self, deadline: Deadline) -> bool:
        """Read until the calibration running ends; whether a read showed it end."""
        while deadline.remaining() > 0:
            again = await self._read_quietly(deadline)
            if again is None:
                return False
            if again.step not in _RUNNING_STEPS:
                return True
            await anyio.sleep(min(_FOLLOW_POLL_S, max(0.0, deadline.remaining())))
        return False

    async def _clean_return(
        self,
        observation: PanelObservation,
        sent: set[tuple[KeyCode | str, int]],
        actions: list[str],
        deadline: Deadline,
    ) -> bool:
        """42002, once, on a screen or step no key belongs to."""
        if ("42002", 0) in sent:
            actions.append("42002 already sent; stopped")
            return False
        sent.add(("42002", 0))
        spec = OPERATIONS["return_to_measurement"]

        async def body(client: ProtocolClient, dl: Deadline) -> None:
            await client.write_register(spec.address, spec.value, deadline=dl, command=spec.name)

        try:
            await self._session.run(
                f"{_OPERATION} cleanup 42002",
                body,
                timeout=max(deadline.remaining(), 0.0),
                tier=spec.safety,
                confirm=True,
            )
        except FujiWriteOutcomeUnknownError as exc:
            if exc.context.extra.get("failure") == FailureKind.CONNECTION.value:
                raise
        actions.append(f"42002 on {_describe(observation)}")
        return True

    async def _settle_flags(self, deadline: Deadline) -> PanelObservation | None:
        """On the measurement step, give a flag that lags the step a moment to clear."""
        until = Deadline.after(
            min(self._timing.flag_wait, max(deadline.remaining(), 0.0)),
            operation=f"{_OPERATION} flags",
        )
        observation: PanelObservation | None = None
        while True:
            observation = await self._read_quietly(deadline)
            if observation is None or observation.step != _STEP.NONE or not _any_flag(observation):
                return observation
            if until.remaining() <= 0:
                return observation
            await anyio.sleep(min(_CONFIRM_POLL_S, max(0.0, until.remaining())))


def _cleanup_summary(report: CleanupReport) -> str:
    return report.error or "; ".join(report.actions) or "no action was taken"


def _panel_refusals(
    observation: PanelObservation, status: StatusRead, *, waiting: bool = False
) -> list[str]:
    """What the panel forbids: before the first key, or (``waiting``) on the wait step."""
    reasons: list[str] = []
    if not waiting:
        display = observation.display
        if display.screen != DisplayScreen.MEASUREMENT:
            reasons.append(f"the front panel shows {_screen_name(display.screen)}")
        elif observation.step != _STEP.NONE:
            reasons.append(f"the front panel is on {_step_name(observation.step)}")
        reasons += calibrating_reasons(status)
    held = [c.value for c, s in status.channels.items() if s.hold]
    if held:
        reasons.append(f"the outputs of {', '.join(held)} are held")
    return reasons


def _describe(observation: PanelObservation) -> str:
    display = observation.display
    if display.screen != DisplayScreen.MEASUREMENT:
        return _screen_name(display.screen)
    parts = [
        _step_name(observation.step) if observation.step != _STEP.NONE else "the measurement screen"
    ]
    if display.cursor_channel is not None and observation.step in _SELECT_STEPS:
        parts.append(f"the cursor on {display.cursor_channel.value}")
    flags = _any_flag(observation)
    if flags:
        parts.append(
            "the calibration flag set on "
            + ", ".join(c.value for c in sorted(flags, key=lambda c: c.number))
        )
    return ", ".join(parts)


def _response_time(
    channel: ChannelId, asserted: Mapping[ChannelId, Gas], times: Mapping[str, int]
) -> float:
    """``channel``'s response time, from its slot when the asserted gases say which.

    NDIR components are numbered by the non-O2 channels before them (design
    §2.6). Without the gases to say, the longest of the five is used.
    """
    longest = float(max(times.values()))
    if asserted.get(channel) is Gas.O2:
        return float(times["response_time.o2"])
    slot = 0
    for number in range(1, channel.number + 1):
        gas = asserted.get(ChannelId.from_number(number))
        if gas is None:
            return longest
        if gas is not Gas.O2:
            slot += 1
    name = f"response_time.ndir{slot}"
    return float(times[name]) if name in times else longest


def calibration_filename(result: RemoteCalibrationResult, serial_number: str | None) -> str:
    """A file name for a run's record: serial, channel, kind and start time."""
    stamp = result.started_at.strftime("%Y%m%dT%H%M%SZ")
    serial = (serial_number or "analyzer").strip() or "analyzer"
    plan = result.plan
    return f"fuji-calibration_{serial}_{plan.channel.value}_{plan.kind.value}_{stamp}.json"


def _check_seconds(name: str, value: object) -> float:
    """``value`` as seconds, refusing anything but a positive finite number."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not (math.isfinite(value) and value > 0)
    ):
        msg = f"{name} must be a positive number of seconds, got {value!r}"
        raise FujiValidationError(msg)
    return float(value)
