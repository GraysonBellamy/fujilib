"""A manual zero or span driven from the host with the panel's keys (design §6.5).

Every run is against the simulator, whose panel follows the bench analyzer
(protocol findings §14, §18). The steadiness rule is shortened so the gas
settles in a fraction of a second.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

from fujilib.devices.keys import (
    CalibrationGas,
    CleanupReport,
    RemoteCalibration,
    RunState,
    _response_time,
    _write_key,
    calibration_filename,
    key_refusal,
)
from fujilib.devices.models import DisplayState, RangeInfo
from fujilib.devices.panel import (
    ManualCalibrationKind,
    ManualCalibrationOutcome,
    PanelObservation,
    calibration_record,
)
from fujilib.devices.steadiness import SteadinessRule
from fujilib.errors import (
    ErrorContext,
    FujiAnalyzerStateError,
    FujiCapabilityError,
    FujiConfirmationRequiredError,
    FujiConnectionError,
    FujiError,
    FujiModbusError,
    FujiTimeoutError,
    FujiValidationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.enums import DisplayScreen, ErrorCode, KeyCode, ManualCalibrationStep
from fujilib.registry.units import Unit
from fujilib.sync import Fuji, SyncPortal
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockAnalyzer, MockRequest, mock_transport
from tests.facade import FC04, analyzer_on, documented_only, replugging
from tests.factories import T0, reading, status

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.panel import ManualCalibrationPlan

pytestmark = pytest.mark.anyio

CH1, CH2, CH3, CH4, CH5 = (ChannelId.from_number(n) for n in range(1, 6))
UP, DOWN, ESC, ENT = KeyCode.UP, KeyCode.DOWN, KeyCode.ESC, KeyCode.ENT
ZERO, SPAN, MODE = KeyCode.ZERO, KeyCode.SPAN, KeyCode.MODE
STEP = ManualCalibrationStep
FC06 = 0x06
KEY_REGISTER = 0x07D0

RULE = SteadinessRule(window_s=0.15, response_factor=0, timeout_s=3)
QUICK: dict[str, Any] = {
    "rule": RULE,
    "interval": 0.02,
    "key_timeout": 0.5,
    "run_timeout": 3.0,
    "cleanup_timeout": 5.0,
}
ZERO_GAS = CalibrationGas(0.0, label="N2, cylinder 1234")


def quick(**overrides: Any) -> dict[str, Any]:
    """:data:`QUICK` with ``overrides``."""
    return {**QUICK, **overrides}


def state_of(run: RemoteCalibration) -> RunState:
    """Where ``run`` is, read afresh each time."""
    return run.state


def claimed(anz: Analyzer) -> bool:
    """Whether a remote calibration holds ``anz``'s panel."""
    return anz.session.panel_claimed


SPAN_GAS = CalibrationGas(20.95, "vol%", label="20.95 % O2 in N2")


def bench(**config: Any) -> MockAnalyzer:
    """The bench analyzer, its panel offering channels 1-3, a calibration taking 0.05 s."""
    options: dict[str, Any] = {
        "panel_channels": (1, 2, 3),
        "manual_calibration_s": 0.05,
        "key_lock_silence_s": 0.3,
    }
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, **(options | config)))


def writes(mock: MockAnalyzer) -> list[tuple[int | None, int]]:
    """``(address, value)`` of every FC06 request."""
    return [
        (e.request.address, e.request.values[0])
        for e in mock.exchanges
        if e.request.function == FC06
    ]


def sent(mock: MockAnalyzer) -> list[int]:
    """The keys written to 42001 that acted."""
    return [k for k, _ in mock.remote_keys]


def on_write(mock: MockAnalyzer, value: int, action: Callable[[MockRequest], None]) -> None:
    """Do ``action`` when the first key write of ``value`` arrives, before it is handled."""
    done: list[bool] = []

    def hook(request: MockRequest) -> None:
        if (
            not done
            and request.function == FC06
            and request.address == KEY_REGISTER
            and request.values == (value,)
        ):
            done.append(True)
            action(request)

    mock.on_request = hook


def after_polls(mock: MockAnalyzer, polls: int, action: Callable[[MockRequest], None]) -> None:
    """Do ``action`` at the ``polls``-th poll from now."""
    count = [0]

    def hook(request: MockRequest) -> None:
        if request.function == FC04 and request.address == 0 and request.count == 61:
            count[0] += 1
            if count[0] == polls:
                action(request)

    mock.on_request = hook


async def plan_of(anz: Analyzer, channel: str, kind: str) -> ManualCalibrationPlan:
    return await anz.plan_manual_calibration(channel, kind)


async def steady_then_calibrate(run: RemoteCalibration) -> None:
    await run.wait_steady()
    await run.calibrate(confirm=True)


# --- Which keys, where -------------------------------------------------------------------------


def observe(
    step: int = 0,
    *,
    cursor: ChannelId | None = CH3,
    zero: Sequence[ChannelId] = (),
    screen: int = DisplayScreen.MEASUREMENT,
) -> PanelObservation:
    shown = STEP(step) if step in set(STEP) else step
    return PanelObservation(
        at=T0,
        display=DisplayState(
            screen=screen, calibration_step=shown, top_channel=CH1, cursor_channel=cursor
        ),
        channels=MappingProxyType({c: status(zero=c in zero) for c in (CH1, CH2, CH3)}),
        calibration_error=False,
        readings=MappingProxyType({CH3: reading(CH3, Gas.O2, 2018, 2)}),
    )


@pytest.mark.parametrize(
    ("key", "panel", "kwargs", "refusal"),
    [
        (MODE, observe(), {}, "0x01 is not one of the calibration keys"),
        (ZERO, observe(screen=DisplayScreen.MENU), {}, "the menu screen, not the measurement"),
        (ZERO, observe(), {}, None),
        (SPAN, observe(), {}, None),
        (ZERO, observe(zero=[CH3]), {}, "a calibration flag is set on CH3"),
        (DOWN, observe(), {}, "DOWN is not sent on none"),
        (ESC, observe(), {}, "ESC is not sent on none"),
        (UP, observe(4), {}, None),
        (DOWN, observe(7), {}, None),
        (ESC, observe(4), {}, None),
        (ZERO, observe(4), {}, "ZERO is not sent on zero channel select"),
        (ENT, observe(4), {}, "needs the channel the cursor must be on"),
        (ENT, observe(4), {"cursor": CH1}, "ENT with the cursor on CH3, not CH1"),
        (ENT, observe(4, cursor=None), {"cursor": CH1}, "on no channel, not CH1"),
        (ENT, observe(4), {"cursor": CH3}, None),
        (ENT, observe(5, zero=[CH3]), {}, "only calibrate() sends it"),
        (ENT, observe(5, zero=[CH3]), {"calibrate": True}, None),
        (ESC, observe(8), {}, None),
        (DOWN, observe(5), {}, "DOWN is not sent on zero wait"),
        (ESC, observe(6), {}, "ESC is not sent on zero running"),
        (ENT, observe(10), {"calibrate": True}, "ENT there can force the calibration"),
        (ESC, observe(10), {}, None),
        (ESC, observe(3), {}, "ESC is not sent on step 3"),
    ],
)
def test_each_key_only_where_it_belongs(
    key: KeyCode, panel: PanelObservation, kwargs: dict[str, Any], refusal: str | None
) -> None:
    found = key_refusal(key, panel, **kwargs)
    if refusal is None:
        assert found is None
    else:
        assert found is not None
        assert refusal in found


class _Client:
    """A client whose FC06 fails as told."""

    def __init__(self, error: FujiError | None) -> None:
        self.error = error
        self.calls: list[tuple[int, int]] = []

    async def write_register(self, address: int, value: int, **_: object) -> None:
        self.calls.append((address, value))
        if self.error is not None:
            raise self.error


async def test_a_key_whose_reply_is_lost_is_left_to_the_reads() -> None:
    lost = FujiWriteOutcomeUnknownError("lost", context=ErrorContext(extra={"failure": "timeout"}))
    client = _Client(lost)
    deadline = anyio_deadline()
    assert await _write_key(client, ZERO, deadline=deadline) is False  # type: ignore[arg-type]
    assert await _write_key(_Client(None), ZERO, deadline=deadline) is True  # type: ignore[arg-type]
    failed = FujiWriteOutcomeUnknownError(
        "port", context=ErrorContext(extra={"failure": "connection"})
    )
    with pytest.raises(FujiWriteOutcomeUnknownError):
        await _write_key(_Client(failed), ZERO, deadline=deadline)  # type: ignore[arg-type]
    client = _Client(None)
    with pytest.raises(FujiValidationError, match="not one of the calibration keys"):
        await _write_key(client, MODE, deadline=deadline)  # type: ignore[arg-type]
    assert client.calls == []


def anyio_deadline() -> Any:
    from fujilib._deadline import Deadline

    return Deadline.after(None, operation="test")


# --- The gas named ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "0"])
def test_a_calibration_gas_is_a_finite_number(value: Any) -> None:
    with pytest.raises(FujiValidationError, match="finite number"):
        CalibrationGas(value)


def test_a_gas_other_than_zero_needs_its_unit() -> None:
    with pytest.raises(FujiValidationError, match="needs its unit"):
        CalibrationGas(20.95)
    assert CalibrationGas(0).unit is None
    assert CalibrationGas(20.95, "VOL%").unit is Unit.VOL_PERCENT
    with pytest.raises(FujiValidationError, match="'furlongs' is not a unit of the ZP series"):
        CalibrationGas(20.95, "furlongs")


async def test_the_gases_must_match_the_plan_before_anything_is_sent() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        zero = await plan_of(anz, "CH1", "zero")  # CH1 and CH2 "at once"
        span = await plan_of(anz, "CH3", "span")
        mock.clear()
        run = anz.manual_calibration(zero, gas=ZERO_GAS, confirm=True)
        assert set(run.gases) == {CH1, CH2}  # one gas for every established channel
        each = anz.manual_calibration(zero, gas={"CH1": ZERO_GAS, CH2: ZERO_GAS}, confirm=True)
        assert set(each.gases) == {CH1, CH2}
        cases: list[tuple[Any, str]] = [
            ({CH1: ZERO_GAS}, "no calibration gas named for CH2"),
            ({CH1: ZERO_GAS, CH2: ZERO_GAS, CH3: ZERO_GAS}, "for CH3, which the plan does not"),
            ({CH1: ZERO_GAS, CH2: 0.0}, "must be a CalibrationGas"),
        ]
        for gas, message in cases:
            with pytest.raises(FujiValidationError, match=message):
                anz.manual_calibration(zero, gas=gas, confirm=True)
        with pytest.raises(FujiValidationError, match="interval must be a positive"):
            anz.manual_calibration(span, gas=SPAN_GAS, interval=0)
        with pytest.raises(FujiValidationError, match="key_timeout must be a positive"):
            anz.manual_calibration(span, gas=SPAN_GAS, key_timeout=float("inf"))
        absent = await plan_of(anz, "CH4", "span")
        with pytest.raises(FujiValidationError, match="none of the channels"):
            anz.manual_calibration(absent, gas=SPAN_GAS)
    assert writes(mock) == []


# --- A run -------------------------------------------------------------------------------------


async def test_a_remote_zero_of_o2() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        mock.clear()
        verdicts: list[object] = []
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, adc=True, **QUICK)
        assert state_of(run) is RunState.NEW
        async with run:
            assert state_of(run) is RunState.WAITING
            assert run.observation is not None
            assert run.observation.step == STEP.ZERO_WAIT
            mock.flow("CH3", 0.0, tau_s=0.02)
            verdict = await run.wait_steady(progress=verdicts.append)
            assert verdict.steady
            assert run.steadiness is verdict
            event = await run.calibrate(confirm=True)
            assert run.event is event
        result = run.result
    assert state_of(run) is RunState.CLOSED
    assert event.outcome is ManualCalibrationOutcome.COMPLETED
    assert (event.kind, event.channels, event.ranges) == (
        ManualCalibrationKind.ZERO,
        (CH3,),
        {CH3: 1},
    )
    assert event.gases == {CH3: 0.0}
    assert event.adc_before is not None
    assert verdicts[-1] is verdict
    # The keys: ZERO, the cursor from the "at once" pair down to CH3, ENT, ENT.
    assert sent(mock) == [ZERO, DOWN, ENT, ENT]
    assert {a for a, _ in writes(mock)} == {KEY_REGISTER}
    assert mock.keys == []  # nothing was pressed at the panel
    assert mock.register("display.key") == (16,)  # a key over Modbus never shows in 30190
    assert result is not None
    assert result.outcome is ManualCalibrationOutcome.COMPLETED
    assert result.calibrating_key_sent
    assert result.cleanup == CleanupReport(clean=True, actions=())
    assert [(k.name, k.step, k.purpose) for k in result.keys] == [
        ("ZERO", STEP.NONE, "open channel selection"),
        ("DOWN", STEP.ZERO_CHANNEL_SELECT, "move the cursor to CH3"),
        ("ENT", STEP.ZERO_CHANNEL_SELECT, "select the channel"),
        ("ENT", STEP.ZERO_WAIT, "calibrate"),
    ]
    assert all(k.acknowledged and k.taken and k.after_s is not None for k in result.keys)
    assert result.samples
    assert all(s.counts is not None and len(s.counts) == 5 for s in result.samples)
    assert result.error is None
    assert run.keys == result.keys


async def test_a_remote_span_with_the_cursor_already_on_the_channel() -> None:
    mock = bench()
    mock.set_register("display.cursor_channel", 2)  # CH3, where the last calibration left it
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK) as run:
            mock.flow("CH3", 20.95)
            await run.wait_steady()
            event = await run.calibrate(confirm=True)
    assert sent(mock) == [SPAN, ENT, ENT]
    assert event.outcome is ManualCalibrationOutcome.COMPLETED
    assert event.gases == {CH3: 20.95}
    assert event.after[CH3].value == 20.95


async def test_a_zero_of_the_at_once_pair_is_approached_going_down() -> None:
    mock = bench()
    mock.set_register("display.cursor_channel", 2)  # CH3
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH2", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            mock.flow("CH1", 0.0)
            mock.flow("CH2", 0.0)
            await run.wait_steady()
            event = await run.calibrate(confirm=True)
    # From CH3 down, round to the pair, which reads as CH1 reached going down.
    assert sent(mock) == [ZERO, DOWN, ENT, ENT]
    assert event.channels == (CH1, CH2)
    assert event.outcome is ManualCalibrationOutcome.COMPLETED


async def test_zero_opening_on_ch1_after_a_return_to_measurement_is_read_not_assumed() -> None:
    mock = bench()
    mock.set_register("display.cursor_channel", 2)
    async with analyzer_on(mock) as (anz, _):
        await anz.return_to_measurement(confirm=True)  # ZERO then opens on the first position
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            pass
    assert sent(mock) == [ZERO, DOWN, ENT, ESC]
    assert run.result is not None
    assert run.result.outcome is ManualCalibrationOutcome.CANCELLED
    assert run.result.cleanup.actions == ("ESC on zero wait",)


async def test_a_lost_reply_to_a_key_is_settled_by_the_reads() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        mock.inject(
            FaultKind.DROP,
            when=lambda r: r.function == FC06 and r.address == KEY_REGISTER and r.values == (ZERO,),
        )
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            first = run.keys[0]
    assert (first.name, first.acknowledged, first.taken) == ("ZERO", False, True)


# --- Refused before the first key ----------------------------------------------------------------


def _set(name: str, value: int) -> Callable[[MockAnalyzer], None]:
    return lambda mock: mock.set_register(name, value)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (_set("key_lock", 1), "key lock is on"),
        (_set("output_hold.enabled", 1), "output hold is on"),
        (_set("display.screen", 1), "the front panel shows the menu screen"),
        (_set("display.screen", 42), "the front panel shows screen 42"),
        (_set("display.calibration_step", 4), "the front panel is on zero channel select"),
        (_set("status.ch3.zero_calibrating", 1), "CH3 is being calibrated"),
        (_set("status.ch3.hold", 1), "the outputs of CH3 are held"),
        (_set("status.instrument_error", 1), "instrument error"),
        (_set("calibration.ch3.zero_mode", 1), "the plan has changed"),
    ],
)
async def test_what_the_analyzer_says_can_refuse_a_run_with_nothing_sent(
    setup: Callable[[MockAnalyzer], None], message: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        setup(mock)
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match=message):
            async with run:
                pass
        assert not anz.session.panel_claimed
    assert writes(mock) == []
    assert run.result is not None
    assert run.result.event is None
    assert run.result.outcome is None
    assert run.result.cleanup == CleanupReport(clean=True, actions=())


@pytest.mark.parametrize(
    ("gas", "message"),
    [
        (
            CalibrationGas(20.9, "vol%"),
            "the gas named is 20.9 vol%, but its span-gas setting is 20.95",
        ),
        (CalibrationGas(20.95, "ppm"), "CH3 range 1 measures in vol%, not ppm"),
    ],
)
async def test_the_gas_named_must_be_the_calibration_gas_setting(
    gas: CalibrationGas, message: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        with pytest.raises(FujiAnalyzerStateError, match=message):
            async with anz.manual_calibration(plan, gas=gas, confirm=True, **QUICK):
                pass
    assert writes(mock) == []


async def test_a_plan_on_both_ranges_is_refused() -> None:
    mock = bench()
    mock.set_register("calibration.ch3.range_mode", 1)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        assert plan.targets[0].widened
        with pytest.raises(FujiAnalyzerStateError, match="set to calibrate both ranges"):
            async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK):
                pass
    assert writes(mock) == []


async def test_a_plan_that_cannot_be_made_again_is_refused() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        mock.set_register("range.ch3.current", 7)
        with pytest.raises(FujiAnalyzerStateError, match="the plan cannot be made again"):
            async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK):
                pass
    assert writes(mock) == []


def test_a_gas_setting_that_does_not_decode_or_a_missing_range_table_refuses() -> None:
    from fujilib.devices.operations import CalibrationTarget
    from fujilib.devices.panel import ManualCalibrationPlan

    target = CalibrationTarget(CH3, (1,), True, False, True, (None,), (None,), ("vol%",))
    plan = ManualCalibrationPlan(ManualCalibrationKind.SPAN, CH3, (target,), ())
    run = RemoteCalibration(object(), plan, gas=SPAN_GAS, confirm=True)  # type: ignore[arg-type]
    table = RangeInfo(CH3, 1, (Unit.VOL_PERCENT,), (21.0,), (2,))
    assert run._gas_refusals([table]) == [
        "CH3 range 1: its calibration-gas setting does not decode"
    ]
    assert run._gas_refusals([]) == ["CH3's range table was not read"]


async def test_a_run_needs_confirm_and_the_a_d_block_when_asked_for() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        mock.clear()
        with pytest.raises(FujiConfirmationRequiredError, match="remote manual calibration"):
            async with anz.manual_calibration(plan, gas=ZERO_GAS):
                pass
        assert not anz.session.panel_claimed
    assert mock.exchanges == []
    documented = MockAnalyzer(documented_only())
    async with analyzer_on(documented) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        documented.clear()
        with pytest.raises(FujiCapabilityError, match="adc_values"):
            async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, adc=True):
                pass
    assert documented.exchanges == []


async def test_one_run_at_a_time_and_nothing_else_writes_meanwhile() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        async with run:
            assert claimed(anz)
            with pytest.raises(FujiAnalyzerStateError, match="already under way"):
                async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK):
                    pass
            with pytest.raises(FujiAnalyzerStateError, match="under way at the front panel"):
                await anz.return_to_measurement(confirm=True)
            with pytest.raises(FujiAnalyzerStateError, match="under way at the front panel"):
                await anz.set_response_time("o2", 15, confirm=True)
        assert not anz.session.panel_claimed
        with pytest.raises(FujiValidationError, match="used once"):
            async with run:
                pass
    assert sent(mock) == [ZERO, DOWN, ENT, ESC]


# --- The panel does not do what was asked --------------------------------------------------------


async def test_a_key_that_key_lock_swallows_stops_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        on_write(mock, ZERO, lambda _: mock.set_register("key_lock", 1))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="ZERO was acknowledged, but the panel"):
            async with run:
                pass
    assert [k for k, _ in mock.swallowed_keys] == [ZERO]
    assert run.result is not None
    assert run.result.keys[0].taken is False
    assert run.result.cleanup.clean
    assert run.result.error is not None
    assert "ZERO was acknowledged" in run.result.error


async def test_a_key_pressed_at_the_panel_meanwhile_stops_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        on_write(mock, DOWN, lambda r: mock.press(ESC, at=r.arrived_at))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="the panel left channel selection"):
            async with run:
                pass
    assert run.result is not None
    assert run.result.cleanup == CleanupReport(clean=True, actions=())
    assert sent(mock) == [ZERO, DOWN]


async def test_a_channel_the_panel_does_not_offer_is_never_selected() -> None:
    mock = bench(panel_channels=(1, 2, 4))
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        with pytest.raises(FujiAnalyzerStateError, match="does not offer it on span"):
            async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK):
                pass
    assert ENT not in sent(mock)
    assert sent(mock)[-1] == ESC  # the cleanup left channel selection


async def test_flags_on_a_channel_the_plan_does_not_calibrate_stop_the_run() -> None:
    # The whole panel: the absent CH4 and CH5 are "at once" with CH1 and CH2 too.
    mock = bench(panel_channels=(1, 2, 3, 4, 5))
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH1", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="flag of CH4, CH5, which the plan"):
            async with run:
                pass
    assert run.result is not None
    assert run.result.cleanup.actions == ("ESC on zero wait",)
    assert all(mock.register(f"status.ch{c}.zero_calibrating") == (0,) for c in range(1, 6))


async def test_an_exception_reply_to_a_key_stops_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        mock.inject(
            FaultKind.EXCEPTION,
            when=lambda r: r.function == FC06 and r.address == KEY_REGISTER,
        )
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiModbusError):
            async with run:
                pass
    assert run.result is not None
    assert run.result.keys == ()
    assert sent(mock) == []


# --- On the wait step --------------------------------------------------------------------------


async def test_a_gas_that_never_settles_times_out_and_is_cancelled() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")  # air stays at the inlet
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiTimeoutError, match=r"not steady within 0.3 s") as info:
            async with run:
                await run.wait_steady(timeout=0.3)
    assert "from the named gas 0" in info.value.context.extra["reasons"][0]
    assert run.result is not None
    assert run.result.outcome is ManualCalibrationOutcome.CANCELLED
    assert run.result.cleanup.actions == ("ESC on zero wait",)
    assert not run.result.calibrating_key_sent
    assert mock.register("status.ch3.zero_calibrating") == (0,)


async def test_the_wait_checks_its_timeout() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK) as run:
            with pytest.raises(FujiValidationError, match="timeout must be a positive"):
                await run.wait_steady(timeout=-1)
            verdict = await run.read(timeout=5)
            assert verdict.reads == 1


async def test_calibrate_needs_confirm_and_a_steady_gas() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            with pytest.raises(FujiConfirmationRequiredError, match="starts a manual calibration"):
                await run.calibrate()
            with pytest.raises(FujiAnalyzerStateError, match="no read on the wait step yet"):
                await run.calibrate(confirm=True)
            await run.read()
            with pytest.raises(FujiAnalyzerStateError, match="from the named gas 0"):
                await run.calibrate(confirm=True)
    assert sent(mock) == [ZERO, DOWN, ENT, ESC]


def _flow_away(mock: MockAnalyzer) -> None:
    mock.flow("CH3", 5.0)


CHANGES: list[tuple[Callable[[MockAnalyzer], object], str]] = [
    (_flow_away, "the gas is not steady with this read"),
    (_set("key_lock", 1), "key lock is on"),
    (_set("status.instrument_error", 1), "instrument error"),
    (_set("calibration_gas.ch3.range1.zero", 3), "plan has changed"),
    (_set("status.ch3.hold", 1), "the outputs of CH3 are held"),
    (_set("reading.ch3.unit", 1), "CH3 reads in ppm, not vol%"),
]


@pytest.mark.parametrize(("change", "message"), CHANGES)
async def test_everything_is_checked_again_before_the_key_that_calibrates(
    change: Callable[[MockAnalyzer], None], message: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            mock.flow("CH3", 0.0)
            await run.wait_steady()
            change(mock)
            with pytest.raises(FujiAnalyzerStateError, match=message):
                await run.calibrate(confirm=True)
            assert run.state is RunState.WAITING
            mock.set_register("key_lock", 0)  # so the cleanup's ESC is taken
    assert sent(mock) == [ZERO, DOWN, ENT, ESC]
    assert run.result is not None
    assert not run.result.calibrating_key_sent


async def test_cancel_leaves_the_wait_step_with_esc() -> None:
    mock = bench(flag_lag_s=0.05)  # the flag clears a moment after the step
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK) as run:
            event = await run.cancel()
            assert run.state is RunState.ENDED
            for call in (run.cancel(), run.wait_steady(), run.calibrate(confirm=True)):
                with pytest.raises(FujiAnalyzerStateError, match="the run is ended"):
                    await call
    assert event is not None
    assert event.outcome is ManualCalibrationOutcome.CANCELLED
    assert event.gases == {CH3: 20.95}
    assert sent(mock) == [SPAN, DOWN, DOWN, ENT, ESC]
    assert run.result is not None
    assert run.result.cleanup == CleanupReport(clean=True, actions=())


async def test_the_operator_leaving_the_wait_step_at_the_panel_ends_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        _polls_after_write(mock, ENT, 2, lambda: mock.press(ESC))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="left the wait step"):
            async with run:
                await run.wait_steady()
    assert run.result is not None
    assert run.result.outcome is ManualCalibrationOutcome.CANCELLED
    assert run.result.cleanup == CleanupReport(clean=True, actions=())


async def test_a_calibration_made_at_the_panel_meanwhile_is_recorded_as_it_ran() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        _polls_after_write(mock, ENT, 2, lambda: mock.press(ENT))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="left the wait step"):
            async with run:
                await run.wait_steady()
    assert run.result is not None
    assert run.result.outcome is ManualCalibrationOutcome.COMPLETED
    assert not run.result.calibrating_key_sent
    assert sent(mock) == [ZERO, DOWN, ENT]


# --- The calibration -------------------------------------------------------------------------


async def test_a_failed_calibration_leaves_the_error_display_with_esc_never_ent() -> None:
    mock = bench()
    mock.calibration_errors = {3: int(ErrorCode.ZERO_AMOUNT_OVER_50)}  # ENT would force it
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            mock.flow("CH3", 0.0)
            await run.wait_steady()
            event = await run.calibrate(confirm=True)
    assert event.outcome is ManualCalibrationOutcome.FAILED
    assert event.new_errors == {CH3: {ErrorCode.ZERO_AMOUNT_OVER_50}}
    assert sent(mock) == [ZERO, DOWN, ENT, ENT, ESC]
    assert run.result is not None
    assert [(k.name, k.step) for k in run.result.keys][-1] == ("ESC", STEP.ERROR_DISPLAY)
    assert run.result.keys[-1].purpose == "leave the error display"


async def test_the_silence_while_a_calibration_is_stored_is_ridden_out() -> None:
    mock = bench(manual_calibration_s=0.3, storing_silence_s=0.25)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK) as run:
            await run.wait_steady()
            event = await run.calibrate(confirm=True)
    assert event.outcome is ManualCalibrationOutcome.COMPLETED
    assert run.result is not None
    assert run.result.cleanup.clean


async def test_a_calibration_that_outlasts_the_run_timeout_is_waited_for_by_the_cleanup() -> None:
    mock = bench(manual_calibration_s=0.6)
    run_quick = quick(run_timeout=0.2)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        run = anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **run_quick)
        with pytest.raises(FujiTimeoutError, match=r"did not end within 0.2 s"):
            async with run:
                await steady_then_calibrate(run)
    assert run.result is not None
    assert run.result.cleanup.actions == ("waited for span running to end",)
    assert run.result.cleanup.clean
    assert run.result.outcome is ManualCalibrationOutcome.COMPLETED


# --- The cleanup -------------------------------------------------------------------------------


async def test_a_flag_left_set_on_the_measurement_screen_raises_with_the_recovery() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        # As 42002 does on the wait step (findings §18.4): the step goes, the flag stays.
        _polls_after_write(mock, ENT, 2, lambda: mock.set_register("display.calibration_step", 0))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="still set") as info:
            async with run:
                await run.wait_steady()
    assert "ZERO, the channel, ENT once (the wait step), then ESC" in str(info.value)
    assert isinstance(info.value.__cause__, FujiAnalyzerStateError)
    assert "left the wait step" in str(info.value.__cause__)
    assert run.result is not None
    assert run.result.cleanup.flags_left == (CH3,)


async def test_a_menu_opened_meanwhile_is_closed_with_42002() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        on_write(mock, DOWN, lambda _: mock.set_register("display.screen", 1))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="DOWN: the panel shows the menu screen"):
            async with run:
                pass
    assert run.result is not None
    assert run.result.cleanup.actions == ("42002 on the menu screen",)
    assert run.result.cleanup.clean
    assert mock.commands == [(0x07D1, 1)]


async def test_42002_is_sent_once_and_its_lost_reply_is_no_failure() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")

        returned: list[bool] = []

        def menu_for_good(request: MockRequest) -> None:
            # A menu at the DOWN, and again after the 42002 closes it.
            if request.function == FC06 and request.address == 0x07D1:
                returned.append(True)
            elif (request.function == FC06 and request.values == (DOWN,)) or (
                returned and request.function == FC04
            ):
                mock.set_register("display.screen", 1)

        mock.on_request = menu_for_good
        mock.inject(FaultKind.DROP, when=lambda r: r.function == FC06 and r.address == 0x07D1)
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="DOWN: the panel shows the menu screen"):
            async with run:
                pass
    # The menu came back after the 42002, which is not sent twice; the key's error stands.
    assert run.result is not None
    assert run.result.cleanup.actions == (
        "42002 on the menu screen",
        "42002 already sent; stopped",
    )
    assert not run.result.cleanup.clean


async def test_an_esc_the_panel_does_not_take_is_not_sent_again() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(
            FujiAnalyzerStateError, match="could not be returned to the measurement"
        ):
            async with run:
                mock.set_register("key_lock", 1)
    assert run.result is not None
    actions = run.result.cleanup.actions
    assert actions[0].startswith("ESC on zero wait: ESC was acknowledged, but")
    assert actions[1] == "ESC already sent on zero wait; stopped"
    assert [k for k, _ in mock.swallowed_keys] == [ESC]
    mock.set_register("key_lock", 0)


async def test_a_run_cancelled_by_the_caller_still_cleans_up() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with anyio.move_on_after(0.2) as scope:
            async with run:
                await run.wait_steady()
        assert scope.cancelled_caught
        assert not anz.session.panel_claimed
    assert run.result is not None
    assert run.result.cleanup.actions == ("ESC on zero wait",)
    assert run.result.error is not None


async def test_a_connection_that_fails_leaves_the_cleanup_undone_and_says_so() -> None:
    mock = bench()
    errors: list[FujiError] = []
    async with replugging(mock) as (anz, cable):
        plan = await plan_of(anz, "CH3", "zero")

        async def pull() -> None:
            await anyio.sleep(0.15)
            await cable.unplug()

        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(pull)
            try:
                async with run:
                    await run.wait_steady()
            except FujiError as exc:
                errors.append(exc)
    assert isinstance(errors[0], FujiConnectionError)
    assert any("was not left clean" in note for note in errors[0].__notes__)
    assert run.result is not None
    assert not run.result.cleanup.clean
    assert run.result.cleanup.error is not None


# --- Records -------------------------------------------------------------------------------------


async def test_a_run_is_recorded_as_a_calibration_document() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(
            plan, gas=ZERO_GAS, confirm=True, adc=True, **QUICK
        ) as run:
            mock.flow("CH3", 0.0)
            await run.wait_steady()
            await run.calibrate(confirm=True)
        info = anz.info
    assert run.result is not None
    record = run.result.as_record(info=info, port="COM8", address=1, operator="GB", notes="n")
    text = json.dumps(record)
    back = json.loads(text)
    assert back["format"] == "fujilib-calibration/1"
    assert back["source"] == "remote"
    assert back["analyzer"]["serial_number"] == "N8A0259T"
    assert (back["kind"], back["outcome"], back["channels"]) == ("zero", "completed", ["CH3"])
    assert back["channel_gases"] == {"CH3": "o2"}
    assert back["calibrated_at"] is not None
    assert back["named_gas"] == {"CH3": {"value": 0.0, "unit": None, "label": "N2, cylinder 1234"}}
    assert back["gas_settings"] == {"CH3": 0.0}
    assert back["steadiness"]["steady"] is True
    assert back["steadiness"]["rule"]["window_s"] == 0.15
    assert back["plan"]["targets"][0]["channel"] == "CH3"
    assert [k["key"] for k in back["keys"]] == ["ZERO", "DOWN", "ENT", "ENT"]
    assert back["wait_series"][0]["counts"] is not None
    assert len(back["detector_counts"]) == 5
    assert back["cleanup"] == {"clean": True, "actions": [], "flags_left": [], "error": None}
    assert (back["operator"], back["notes"], back["error"]) == ("GB", "n", None)
    name = calibration_filename(run.result, "N8A0259T")
    assert name.startswith("fuji-calibration_N8A0259T_CH3_zero_")
    assert name.endswith("Z.json")
    assert calibration_filename(run.result, None).startswith("fuji-calibration_analyzer_CH3")


async def test_a_run_refused_before_any_key_is_recorded_without_an_event() -> None:
    mock = bench()
    mock.set_register("key_lock", 1)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError):
            async with run:
                pass
    assert run.result is not None
    record = run.result.as_record()
    assert (record["kind"], record["outcome"], record["keys"]) == ("zero", None, [])
    assert cast("dict[str, object]", record["steadiness"])["steady"] is None
    assert record["analyzer"] == {
        "model": None,
        "serial_number": None,
        "type_code": None,
        "port": None,
        "address": None,
    }


async def test_a_calibration_watched_at_the_panel_has_the_same_record() -> None:
    mock = bench()
    keys = [None, ZERO, DOWN, ENT, ENT]

    def operator(request: MockRequest) -> None:
        if keys and request.function == FC04 and request.address == 0 and request.count == 61:
            key = keys.pop(0)
            if key is not None:
                mock.press(key, at=request.arrived_at)

    mock.on_request = operator
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    record = calibration_record(event, info=anz.info, port=anz.port, address=1)
    assert record["source"] == "panel"
    assert record["outcome"] == "completed"
    assert record["detector_counts"] is None
    before = cast("dict[str, dict[str, object]]", record["before"])
    assert before["CH3"]["unit"] == "vol%"
    json.dumps(record)


def test_the_response_time_follows_the_asserted_gases() -> None:
    times = {
        "response_time.o2": 20,
        "response_time.ndir1": 10,
        "response_time.ndir2": 12,
        "response_time.ndir3": 30,
        "response_time.ndir4": 40,
    }
    bench_map = {CH1: Gas.CO2, CH2: Gas.CO, CH3: Gas.O2}
    assert _response_time(CH3, bench_map, times) == 20
    assert _response_time(CH1, bench_map, times) == 10
    assert _response_time(CH2, bench_map, times) == 12
    assert _response_time(CH2, {CH2: Gas.CO}, times) == 40  # CH1's gas unknown: the longest
    o2_first = {CH1: Gas.O2, CH2: Gas.CO2, CH3: Gas.CO, CH4: Gas.CO2, CH5: Gas.CO2}
    assert _response_time(CH2, o2_first, times) == 10
    assert _response_time(CH5, o2_first, times) == 40


# --- Blocking -----------------------------------------------------------------------------------


def test_a_blocking_remote_calibration() -> None:
    mock = bench()
    with (
        SyncPortal() as portal,
        portal.wrap_async_context_manager(mock_transport(mock)) as (transport, _line),
        Fuji.open(
            transport, portal=portal, channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}
        ) as anz,
    ):
        plan = anz.plan_manual_calibration("CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with run:
            assert run.state is RunState.WAITING
            assert run.run.plan is plan
            assert (run.plan, run.rule) == (plan, RULE)
            assert set(run.gases) == {CH3}
            assert run.observation is not None
            mock.flow("CH3", 0.0, at=0.0)
            assert run.read().reads == 1
            verdict = run.wait_steady(timeout=3)
            assert run.steadiness is verdict
            event = run.calibrate(confirm=True)
            assert run.event is event
            assert len(run.keys) == 4
        assert run.result is not None
        assert run.result.outcome is ManualCalibrationOutcome.COMPLETED
        with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as again:
            assert again.cancel() is not None
        assert again.state is RunState.CLOSED


# --- The panel between reads ---------------------------------------------------------------------


def _polls_after_write(
    mock: MockAnalyzer, value: int, polls: int, action: Callable[[], None]
) -> None:
    """Do ``action`` at the ``polls``-th poll after the first key write of ``value``."""
    state = {"armed": False, "polls": 0}

    def hook(request: MockRequest) -> None:
        if (
            request.function == FC06
            and request.address == KEY_REGISTER
            and request.values == (value,)
        ):
            state["armed"] = True
        elif (
            state["armed"]
            and request.function == FC04
            and request.address == 0
            and request.count == 61
        ):
            state["polls"] += 1
            if state["polls"] == polls:
                action()

    mock.on_request = hook


async def test_a_key_the_panel_no_longer_allows_is_refused_by_its_own_read() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        # ZERO's first read confirms it; the DOWN's own read then finds the panel left.
        _polls_after_write(mock, ZERO, 2, lambda: mock.press(ESC))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="DOWN refused, nothing was sent"):
            async with run:
                pass
    assert sent(mock) == [ZERO]


@pytest.mark.parametrize(
    ("key", "shows"),
    [
        (ZERO, "(the panel shows the measurement screen)"),
        (DOWN, "(the panel shows zero channel select, the cursor on CH1)"),
        (ENT, "(the panel shows zero channel select, the cursor on CH3)"),
    ],
)
async def test_a_key_the_panel_acknowledges_and_ignores_says_what_the_panel_shows(
    key: KeyCode, shows: str
) -> None:
    mock = bench(key_lock_silence_s=0.0)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        on_write(mock, key, lambda _: mock.set_register("key_lock", 1))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="was acknowledged, but the panel") as info:
            async with run:
                mock.set_register("key_lock", 0)
        mock.set_register("key_lock", 0)
    assert shows in str(info.value)


async def test_an_ent_that_does_not_open_the_wait_step_stops_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        on_write(mock, ENT, lambda r: mock.press(ESC, at=r.arrived_at))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="it went to none, not zero wait"):
            async with run:
                pass


async def test_the_key_that_calibrates_must_be_seen_taken() -> None:
    mock = bench(key_lock_silence_s=0.0)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK) as run:
            await run.wait_steady()

            def lock_at_the_second_ent(request: MockRequest) -> None:
                if request.function == FC06 and request.values == (ENT,):
                    mock.set_register("key_lock", 1)

            mock.on_request = lock_at_the_second_ent
            with pytest.raises(FujiAnalyzerStateError, match="ENT was acknowledged, but"):
                await run.calibrate(confirm=True)
            mock.set_register("key_lock", 0)
            mock.on_request = None
            # A key not seen taken ends the run: no second key that calibrates.
            assert state_of(run) is RunState.ENDED
            with pytest.raises(FujiAnalyzerStateError, match="the run is ended"):
                await run.calibrate(confirm=True)
    assert run.result is not None
    assert run.result.calibrating_key_sent  # sent, and swallowed
    assert run.result.outcome is ManualCalibrationOutcome.CANCELLED
    assert [k for k, _ in mock.swallowed_keys] == [ENT]


async def test_a_key_that_calibrates_and_lands_elsewhere_stops_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        async with anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **QUICK) as run:
            await run.wait_steady()

            # The calibration starts; the next read shows channel selection instead.
            _polls_after_write(
                mock, ENT, 1, lambda: mock.set_register("display.calibration_step", 7)
            )
            with pytest.raises(FujiAnalyzerStateError, match="it went to span channel select"):
                await run.calibrate(confirm=True)
            mock.on_request = None
    assert run.result is not None


async def test_a_silence_longer_than_the_run_timeout_is_left_to_the_cleanup() -> None:
    mock = bench(manual_calibration_s=1.0, storing_silence_s=0.9)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        run = anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **quick(run_timeout=0.4))
        with pytest.raises(FujiTimeoutError, match=r"did not end within 0.4 s"):
            async with run:
                await steady_then_calibrate(run)
    assert run.result is not None
    assert run.result.cleanup.clean
    assert run.result.outcome is ManualCalibrationOutcome.COMPLETED


async def test_a_cleanup_that_runs_out_of_time_says_so() -> None:
    # The run's end falls silent after the first read the cleanup makes.
    mock = bench(manual_calibration_s=1.2, storing_silence_s=0.8)
    shorter = quick(run_timeout=0.2, cleanup_timeout=0.5)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        run = anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **shorter)
        with pytest.raises(FujiTimeoutError, match="did not end"):
            async with run:
                await steady_then_calibrate(run)
        await anyio.sleep(1.2)  # let the simulated calibration finish before closing
    assert run.result is not None
    assert not run.result.cleanup.clean
    assert run.result.cleanup.actions == ("waited for span running to end",)


async def test_a_run_waited_for_until_the_cleanup_runs_out_is_not_clean() -> None:
    mock = bench(manual_calibration_s=1.0)
    shorter = quick(run_timeout=0.2, cleanup_timeout=0.3)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "span")
        run = anz.manual_calibration(plan, gas=SPAN_GAS, confirm=True, **shorter)
        with pytest.raises(FujiTimeoutError, match="did not end"):
            async with run:
                await steady_then_calibrate(run)
        await anyio.sleep(1.0)
    assert run.result is not None
    assert not run.result.cleanup.clean
    assert run.result.cleanup.flags_left == ()


async def test_a_panel_that_cannot_be_read_during_the_cleanup_is_not_clean() -> None:
    mock = bench(key_lock_silence_s=2.0)
    shorter = quick(cleanup_timeout=0.8)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **shorter)
        with pytest.raises(FujiAnalyzerStateError, match="could not be returned"):
            async with run:
                mock.set_register("key_lock", 1)
        await anyio.sleep(2.0)
        mock.set_register("key_lock", 0)
    assert run.result is not None
    assert run.result.cleanup.error == "the panel could not be read"


async def test_a_port_that_fails_while_a_key_is_confirmed_breaks_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        client = anz.session._client  # the port fails under the session
        real = client.read_plan
        failing = {"on": False}

        async def read_plan(*args: Any, **kwargs: Any) -> Any:
            if failing["on"]:
                raise FujiConnectionError("the port went away")
            return await real(*args, **kwargs)

        monkeypatch.setattr(client, "read_plan", read_plan)
        on_write(mock, ZERO, lambda _: failing.update(on=True))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiConnectionError, match="went away"):
            async with run:
                pass
    assert run.result is not None
    assert not run.result.cleanup.clean


async def test_a_port_that_fails_as_42002_is_sent_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        client = anz.session._client
        real = client.write_register

        async def write_register(address: int, value: int, **kwargs: Any) -> Any:
            if address == 0x07D1:
                context = ErrorContext(extra={"failure": "connection"})
                raise FujiWriteOutcomeUnknownError("the port went away", context=context)
            return await real(address, value, **kwargs)

        monkeypatch.setattr(client, "write_register", write_register)
        on_write(mock, DOWN, lambda _: mock.set_register("display.screen", 1))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="DOWN: the panel shows the menu"):
            async with run:
                pass
        mock.set_register("display.screen", 0)
    assert run.result is not None
    assert run.result.cleanup.error is not None
    assert "went away" in run.result.cleanup.error


async def test_a_key_that_opens_another_step_stops_the_run() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        _polls_after_write(mock, ZERO, 1, lambda: mock.set_register("display.calibration_step", 7))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="opened span channel select, not zero"):
            async with run:
                pass
    assert run.result is not None
    assert run.result.cleanup.actions == ("ESC on span channel select",)


async def test_an_esc_that_lands_elsewhere_is_left_to_the_cleanup() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            _polls_after_write(
                mock, ESC, 1, lambda: mock.set_register("display.calibration_step", 4)
            )
            with pytest.raises(FujiAnalyzerStateError, match="ESC: it went to zero channel"):
                await run.cancel()
            mock.on_request = None
    assert run.result is not None
    assert run.result.cleanup.actions == ("ESC on zero channel select",)
    assert run.result.cleanup.clean


async def test_a_port_that_fails_as_a_key_is_written_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        client = anz.session._client

        async def write_register(address: int, value: int, **kwargs: Any) -> Any:
            context = ErrorContext(extra={"failure": "connection"})
            raise FujiWriteOutcomeUnknownError("the port went away", context=context)

        monkeypatch.setattr(client, "write_register", write_register)
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiWriteOutcomeUnknownError, match="went away"):
            async with run:
                pass
    assert run.result is not None
    assert [(k.name, k.acknowledged, k.taken) for k in run.result.keys] == [("ZERO", False, False)]
    assert not run.result.cleanup.clean  # the session broke: nothing could be read after it


async def test_a_key_cancelled_while_it_is_written_is_still_cleaned_up_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fujilib.devices import keys as keys_module

    mock = bench()
    written = anyio.Event()
    real = keys_module._write_key
    slow: list[bool] = []

    async def write_then_wait(client: Any, key: KeyCode, *, deadline: Any) -> bool:
        acknowledged = await real(client, key, deadline=deadline)
        if not slow:  # the first key only: its reply never seems to come
            slow.append(True)
            written.set()
            await anyio.sleep_forever()
        return acknowledged

    monkeypatch.setattr(keys_module, "_write_key", write_then_wait)
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as tg:

                async def cancel_once_written() -> None:
                    await written.wait()
                    scope.cancel()

                _ = tg.start_soon(cancel_once_written)
                async with run:
                    pass
        assert not claimed(anz)
    assert run.result is not None
    assert (run.result.keys[0].name, run.result.keys[0].acknowledged) == ("ZERO", False)
    assert run.result.cleanup.actions == ("ESC on zero channel select",)
    assert run.result.cleanup.clean
    assert run.result.error == "CancelledError" or run.result.error


async def test_a_failed_read_before_a_cleanup_esc_does_not_end_the_cleanup() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        async with anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK) as run:
            polls = [0]

            def silence_the_esc_read(request: MockRequest) -> None:
                if request.function == FC04 and request.address == 0 and request.count == 61:
                    polls[0] += 1
                    if polls[0] == 2:  # the cleanup's first read, then the ESC's own read
                        mock._silences.append((request.arrived_at, request.arrived_at + 1.0))

            mock.on_request = silence_the_esc_read
    assert run.result is not None
    actions = run.result.cleanup.actions
    assert actions[0].startswith("ESC on zero wait: ")
    assert actions[-1] == "ESC on zero wait"
    assert run.result.cleanup.clean
    assert sent(mock).count(ESC) == 1


async def test_an_interrupted_cleanup_still_leaves_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)

        async def interrupted() -> CleanupReport:
            raise RuntimeError("interrupted")

        with pytest.raises(RuntimeError, match="interrupted"):
            async with run:
                monkeypatch.setattr(run, "_clean_up", interrupted)
        assert not claimed(anz)
        monkeypatch.undo()
        mock.press(ESC)  # the test's own cleanup
    assert run.result is not None
    assert run.result.cleanup.error == "the cleanup was interrupted before it finished"


async def test_keys_go_only_on_the_plans_own_step() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        # After ZERO is taken, the panel shows span channel selection instead.
        _polls_after_write(mock, ZERO, 2, lambda: mock.set_register("display.calibration_step", 7))
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)
        with pytest.raises(FujiAnalyzerStateError, match="DOWN refused, nothing was sent: it goes"):
            async with run:
                pass
    assert sent(mock) == [ZERO, ESC]


async def test_a_port_that_fails_as_the_cleanup_sends_esc_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await plan_of(anz, "CH3", "zero")
        run = anz.manual_calibration(plan, gas=ZERO_GAS, confirm=True, **QUICK)

        async def gone(*_: object, **__: object) -> PanelObservation:
            raise FujiConnectionError("the port went away")

        with pytest.raises(FujiAnalyzerStateError, match="could not be returned"):
            async with run:
                monkeypatch.setattr(run, "_escape", gone)
        monkeypatch.undo()
        mock.press(ESC)  # the test's own cleanup
    assert run.result is not None
    assert run.result.cleanup.error is not None
    assert "went away" in run.result.cleanup.error
