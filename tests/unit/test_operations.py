"""Operation commands, calibration plans and status, against the simulated analyzer.

Design §6.2-§6.4. The simulator runs auto calibration and auto zero calibration
on a fast clock (``time_scale``); every refusal is checked for sending nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import anyserial
import pytest

from fujilib.devices.capability import Capability
from fujilib.devices.models import DisplayState, ReadingState
from fujilib.devices.operations import (
    PLAN_SETTINGS,
    CalibrationRun,
    CalibrationStatus,
    CommandOutcome,
    _outcome,
)
from fujilib.devices.profile import ZP_PROFILE
from fujilib.devices.session import Session, SessionState
from fujilib.errors import (
    FujiAnalyzerStateError,
    FujiCapabilityError,
    FujiConfirmationRequiredError,
    FujiConnectionError,
    FujiDecodeError,
    FujiTimeoutError,
    FujiValidationError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.protocol.modbus.read_plan import plan_reads
from fujilib.registry.channels import ChannelId
from fujilib.registry.enums import DisplayScreen, ErrorCode, ManualCalibrationStep
from fujilib.registry.registers import REGISTRY
from fujilib.sinks import sample_to_row
from fujilib.streaming import PollSourceAdapter, record
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockAnalyzer, MockRequest, mock_port
from tests.conftest import approx
from tests.facade import FC04, POLL, analyzer_on, bench, when

pytestmark = pytest.mark.anyio

FC06 = 0x06
CH1, CH2, CH3, CH4, CH5 = (ChannelId.from_number(n) for n in range(1, 6))
AUTO = Capability.AUTO_CALIBRATION | Capability.AUTO_ZERO
PLAN = [b.key for b in plan_reads([REGISTRY.resolve(n) for n in PLAN_SETTINGS])]
AUTO_CAL, AUTO_ZERO, BLOWBACK, MEASURE = 0x07D2, 0x07D3, 0x07D4, 0x07D1
ESC_KEY, ENT_KEY, ZERO_KEY, SPAN_KEY = 0x10, 0x20, 0x40, 0x80


def fast(scale: float = 0.0001) -> MockAnalyzer:
    """The bench analyzer, running calibrations at ``scale`` times their flow times."""
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, time_scale=scale))


def with_type_code(code: str) -> MockAnalyzer:
    words = dict(DEFAULT_ZPA_BANK.input)
    words.update(zip(range(0x448, 0x448 + 26), encode_chars(code.ljust(26), 26), strict=True))
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, input=words))


# --- Plans ---------------------------------------------------------------------------------


async def test_the_auto_calibration_plan_of_the_bench_analyzer() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        plan = await anz.plan_auto_calibration()
    assert mock.transactions() == PLAN
    assert plan.run is CalibrationRun.AUTO_CALIBRATION
    assert plan.channels == (CH1, CH2, CH3, CH4, CH5)
    ch3 = plan.targets[2]
    assert (ch3.ranges, ch3.span, ch3.widened, ch3.established) == ((1,), True, False, True)
    assert ch3.zero_gas == (0.0,)
    assert ch3.span_gas[0] == approx(20.95)
    assert ch3.units == ("vol%",)
    assert not plan.targets[3].established
    assert plan.hold is False
    assert plan.phases == (("zero", 300), *((f"CH{c} span", 300) for c in range(1, 6)))
    assert plan.estimated_duration_s == 1800
    assert any("CH4, CH5 enabled but not established" in n for n in plan.notes)
    assert any("inferred" in n for n in plan.notes)


async def test_both_ranges_and_hold_widen_and_lengthen_the_plan() -> None:
    mock = bench()
    mock.set_register("calibration.ch3.range_mode", 1)  # both
    mock.set_register("calibration.ch1.range_mode", 1)  # both, but Ch1 has one range
    mock.set_register("auto_calibration.ch2.range", 1)  # range 2 of a one-range channel
    mock.set_register("output_hold.enabled", 1)
    mock.set_register("auto_calibration.flow_time7", 120)
    for c in (4, 5):
        mock.set_register(f"auto_calibration.ch{c}.included", 0)
    async with analyzer_on(mock) as (anz, _):
        plan = await anz.plan_auto_calibration()
        zero_plan = await anz.plan_auto_zero_calibration()
    ch1, ch2, ch3 = plan.targets
    assert (ch3.ranges, ch3.widened) == ((1, 2), True)
    assert ch3.span_gas[0] == approx(20.95)
    assert ch3.span_gas[1] == approx(20.01)
    assert (ch1.ranges, ch1.widened) == ((1,), False)
    assert ch2.ranges == (2,)
    assert any("CH2 is set to auto-calibrate range 2, but it has 1 range" in n for n in plan.notes)
    assert plan.phases[-1] == ("hold extension", 120)
    assert plan.estimated_duration_s == 300 * 4 + 120
    assert zero_plan.run is CalibrationRun.AUTO_ZERO
    assert not any(t.span for t in zero_plan.targets)
    assert zero_plan.phases == (("zero", 300), ("gas replacement", 300))
    assert zero_plan.estimated_duration_s == 600


# --- Starting a calibration ---------------------------------------------------------------


async def test_auto_calibration_needs_confirm() -> None:
    mock = bench()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        with pytest.raises(
            FujiConfirmationRequiredError, match="start_auto_calibration is DANGEROUS"
        ):
            await anz.start_auto_calibration()
    assert mock.exchanges == []


async def test_auto_calibration_needs_the_option() -> None:
    # The bench unit's type code lists the FAULT contact only (digit 22 = A).
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        assert anz.options is Capability.NONE
        with pytest.raises(FujiCapabilityError, match="does not list the auto calibration option"):
            await anz.start_auto_calibration(confirm=True)
        with pytest.raises(FujiCapabilityError, match="auto zero option"):
            await anz.start_auto_zero_calibration(confirm=True)
    assert mock.exchanges == []


async def test_a_type_code_that_lists_the_option_is_enough() -> None:
    mock = with_type_code("ZPACBJY1MPFYYYYYY2DEYEYAY0")  # DIO E: auto calibration and alarms
    async with analyzer_on(mock) as (anz, _):
        assert anz.options == Capability.ALARMS | AUTO
        assert anz.session.asserted_options is Capability.NONE
        result = await anz.start_auto_zero_calibration(confirm=True)
    assert result.outcome is CommandOutcome.STARTED


async def test_before_identify_an_option_is_refused_even_when_asserted() -> None:
    # The model decides what options there can be: no ZPA has blowback.
    mock = bench()
    async with analyzer_on(mock, identify=False, options=AUTO | Capability.BLOWBACK) as (anz, _):
        with pytest.raises(FujiCapabilityError, match=r"known before identify\(\)"):
            await anz.start_auto_calibration(confirm=True)
        with pytest.raises(FujiCapabilityError, match="identify the analyzer first"):
            await anz.start_blowback(confirm=True)
    assert mock.exchanges == []


async def test_auto_calibration_starts_and_reports_its_plan() -> None:
    mock = fast()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        result = await anz.start_auto_calibration(confirm=True)
    assert mock.transactions() == POLL + PLAN + [(FC06, AUTO_CAL, 1)] + POLL
    assert mock.commands == [(AUTO_CAL, 1)]
    assert result.outcome is CommandOutcome.STARTED
    assert result.acknowledged
    assert result.timing is not None
    assert result.status is not None
    assert result.status.running
    assert result.status.busy
    assert result.plan is not None
    assert result.plan.channels == (CH1, CH2, CH3, CH4, CH5)
    assert result.operation == "start_auto_calibration"
    assert result.before is not None
    assert not result.before.busy


async def test_auto_calibration_is_refused_while_busy_or_faulty() -> None:
    mock = bench()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        mock.set_register("status.ch3.zero_calibrating", 1)
        with pytest.raises(FujiAnalyzerStateError, match="CH3 is being calibrated"):
            await anz.start_auto_calibration(confirm=True)
        mock.set_register("status.ch3.zero_calibrating", 0)
        mock.set_register("status.instrument_error", 1)
        mock.set_register("error.e2.active", 1)
        with pytest.raises(FujiAnalyzerStateError, match=r"instrument error \(2\)"):
            await anz.start_auto_zero_calibration(confirm=True)
        mock.set_register("error.e2.active", 0)
        with pytest.raises(FujiAnalyzerStateError, match="unnumbered"):
            await anz.start_auto_zero_calibration(confirm=True)
    assert FC06 not in {r.request.function for r in mock.exchanges}


async def test_an_acknowledged_calibration_that_does_not_run_is_ambiguous() -> None:
    mock = fast()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        mock.inject(FaultKind.IGNORE, when=when(FC06, AUTO_CAL))
        result = await anz.start_auto_calibration(confirm=True)
    assert result.outcome is CommandOutcome.AMBIGUOUS
    assert result.status is not None
    assert not result.status.busy


async def test_a_calibration_whose_reply_is_lost_is_established_by_the_status() -> None:
    mock = fast(scale=0.01)  # 18 s: still running after the lost reply's timeout
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, AUTO_CAL))
        result = await anz.start_auto_calibration(confirm=True)
    assert result.outcome is CommandOutcome.STARTED
    assert not result.acknowledged
    assert result.timing is None
    mock.finish_calibration()


async def test_a_lost_reply_and_an_idle_status_leave_the_outcome_unknown() -> None:
    mock = fast()

    def end_it(request: MockRequest) -> None:
        if request.function == FC04:
            mock.finish_calibration()

    async with analyzer_on(mock, options=AUTO) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, AUTO_ZERO))
        mock.on_request = end_it
        with pytest.raises(FujiWriteOutcomeUnknownError, match="does not show what it did") as info:
            await anz.start_auto_zero_calibration(confirm=True)
    assert info.value.context.extra["write_state"] == "unknown"


def _lose_status_after(mock: MockAnalyzer) -> None:
    def arm(request: MockRequest) -> None:
        if request.function == FC06:
            mock.inject(FaultKind.DROP, times=None, when=lambda r: r.function == FC04)

    mock.on_request = arm


async def test_a_lost_reply_and_a_failed_status_read_leave_the_outcome_unknown() -> None:
    mock = fast()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, AUTO_CAL))
        _lose_status_after(mock)
        with pytest.raises(FujiWriteOutcomeUnknownError, match="cannot be read"):
            await anz.start_auto_calibration(confirm=True)


async def test_an_acknowledged_command_whose_status_read_fails_is_sent() -> None:
    mock = fast()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        _lose_status_after(mock)
        result = await anz.start_auto_calibration(confirm=True)
    assert result.outcome is CommandOutcome.SENT
    assert result.status is None
    assert result.status_error is not None


async def test_a_port_that_fails_during_a_command_breaks_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = fast()
    async with analyzer_on(mock) as (anz, _):
        stream = anz.session._port.transport.stream
        assert isinstance(stream, anyserial.SerialPort)
        cls = type(stream)
        original = cls.drain
        count = 0

        async def drain(self: anyserial.SerialPort) -> None:
            nonlocal count
            count += 1
            # The status read before the command goes out; the command does not.
            if count > len(POLL):
                raise anyserial.SerialError("device reports an I/O error")
            await original(self)

        monkeypatch.setattr(cls, "drain", drain)
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await anz.return_to_measurement(confirm=True)
        assert anz.session.state is SessionState.BROKEN
    assert info.value.context.extra["failure"] == "connection"


# --- Return to measurement and blowback ----------------------------------------------------


async def test_return_to_measurement_leaves_a_menu_and_is_checked() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.set_register("display.screen", int(DisplayScreen.MAINTENANCE))
        with pytest.raises(FujiConfirmationRequiredError, match="STATEFUL"):
            await anz.return_to_measurement()
        assert mock.exchanges == []
        result = await anz.return_to_measurement(confirm=True)
    assert mock.transactions() == [*POLL, (FC06, MEASURE, 1), *POLL]
    assert result.outcome is CommandOutcome.DONE
    assert result.plan is None
    assert result.before is not None
    assert result.before.display is not None
    assert result.before.display.screen is DisplayScreen.MAINTENANCE
    assert result.status is not None
    assert result.status.display is not None
    assert result.status.display.screen is DisplayScreen.MEASUREMENT


def panel() -> MockAnalyzer:
    """The bench analyzer with its front panel's three measured channels."""
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, panel_channels=(1, 2, 3)))


async def test_return_to_measurement_is_refused_while_a_manual_calibration_waits() -> None:
    mock = panel()
    async with analyzer_on(mock) as (anz, _):
        mock.press(SPAN_KEY)
        mock.press(ENT_KEY)  # the wait step: the channel's span flag is set
        with pytest.raises(FujiAnalyzerStateError, match="CH1 is being calibrated") as info:
            await anz.return_to_measurement(confirm=True)
        assert "ESC on the wait step cancels it" in str(info.value)
        assert mock.transactions() == POLL
        mock.press(ESC_KEY)  # the operator cancels at the panel
        mock.clear()
        result = await anz.return_to_measurement(confirm=True)
    assert mock.transactions() == [*POLL, (FC06, MEASURE, 1), *POLL]
    assert result.outcome is CommandOutcome.DONE


async def test_return_to_measurement_closes_a_manual_calibrations_channel_selection() -> None:
    mock = panel()
    async with analyzer_on(mock) as (anz, _):
        mock.press(ZERO_KEY)
        result = await anz.return_to_measurement(confirm=True)
    assert result.before is not None
    assert result.before.display is not None
    assert result.before.display.calibration_step is ManualCalibrationStep.ZERO_CHANNEL_SELECT
    assert result.outcome is CommandOutcome.DONE
    assert mock.register("display.calibration_step") == (0,)


async def test_return_to_measurement_is_refused_during_an_auto_calibration() -> None:
    mock = fast(scale=1.0)
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        await anz.start_auto_zero_calibration(confirm=True)
        with pytest.raises(FujiAnalyzerStateError, match="auto zero calibration is running"):
            await anz.return_to_measurement(confirm=True)
    assert mock.commands == [(AUTO_ZERO, 1)]


async def test_a_flag_set_meanwhile_is_not_done() -> None:
    mock = panel()

    def operator(request: MockRequest) -> None:
        if request.function == FC06:  # ENT at the panel as the command arrives
            mock.set_register("status.ch3.zero_calibrating", 1)

    async with analyzer_on(mock) as (anz, _):
        mock.on_request = operator
        with pytest.raises(FujiVerificationError, match="but a calibration flag is set"):
            await anz.return_to_measurement(confirm=True)
    assert mock.commands == [(MEASURE, 1)]


async def test_return_to_measurement_that_is_ignored_is_a_mismatch() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.set_register("display.screen", int(DisplayScreen.MENU))
        mock.inject(FaultKind.IGNORE, when=when(FC06, MEASURE))
        with pytest.raises(FujiVerificationError, match="does not show the measurement screen"):
            await anz.return_to_measurement(confirm=True)
        mock.inject(FaultKind.DROP, when=when(FC06, MEASURE))
        result = await anz.return_to_measurement(confirm=True)
    assert result.outcome is CommandOutcome.DONE
    assert not result.acknowledged


async def test_blowback_is_not_a_zpa_feature_even_when_asserted() -> None:
    mock = bench()
    async with analyzer_on(mock, options=Capability.BLOWBACK) as (anz, _):
        assert Capability.BLOWBACK not in anz.options
        assert anz.session.asserted_options is Capability.BLOWBACK
        with pytest.raises(FujiCapabilityError, match="the ZPA has no blowback"):
            await anz.start_blowback(confirm=True)
    assert mock.exchanges == []


async def test_blowback_on_another_model_needs_the_option_asserted() -> None:
    mock = with_type_code("ZPBXXXXX")
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiCapabilityError, match="does not say whether the blowback option"):
            await anz.start_blowback(confirm=True)
    mock = with_type_code("ZPBXXXXX")
    async with analyzer_on(mock, options=Capability.BLOWBACK) as (anz, _):
        result = await anz.start_blowback(confirm=True)
        mock.inject(FaultKind.DROP, when=when(FC06, BLOWBACK))
        with pytest.raises(FujiWriteOutcomeUnknownError, match="does not show what it did"):
            await anz.start_blowback(confirm=True)
    assert result.outcome is CommandOutcome.SENT
    assert mock.commands == [(BLOWBACK, 1), (BLOWBACK, 1)]


async def test_options_must_be_options() -> None:
    async with mock_port(bench()) as (port, _line):
        with pytest.raises(FujiValidationError, match="option capabilities"):
            Session(port, address=1, profile=ZP_PROFILE, options=Capability.CLOCK)


def test_an_outcome_without_a_display_is_not_done() -> None:
    status = CalibrationStatus(False, {}, False, None, datetime.now(UTC))
    with pytest.raises(FujiVerificationError):
        _outcome("return_to_measurement", status, acknowledged=True, read_error=FujiTimeoutError())


def test_a_lost_reply_with_a_flag_set_is_not_done() -> None:
    measuring = DisplayState(DisplayScreen.MEASUREMENT, ManualCalibrationStep.NONE, None, None)
    running = CalibrationStatus(True, {}, False, measuring, datetime.now(UTC))
    with pytest.raises(FujiVerificationError, match="but a calibration flag is set"):
        _outcome(
            "return_to_measurement", running, acknowledged=False, read_error=FujiTimeoutError()
        )


def test_outcomes_the_status_cannot_settle() -> None:
    menu = DisplayState(DisplayScreen.MENU, ManualCalibrationStep.NONE, None, None)
    in_menu = CalibrationStatus(False, {}, False, menu, datetime.now(UTC))
    with pytest.raises(FujiWriteOutcomeUnknownError, match="does not show what it did"):
        _outcome(
            "return_to_measurement", in_menu, acknowledged=False, read_error=FujiTimeoutError()
        )
    with pytest.raises(FujiWriteOutcomeUnknownError, match="cannot be read") as info:
        _outcome(
            "start_auto_calibration",
            None,
            acknowledged=False,
            read_error=FujiConnectionError("the port is gone"),
        )
    assert info.value.context.extra["failure"] == "connection"


# --- Status and waiting ----------------------------------------------------------------------


async def test_calibration_status_and_waiting_for_a_calibration() -> None:
    mock = fast()
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        idle = await anz.calibration_status()
        assert not idle.busy
        assert idle.held == ()
        assert idle.errors == {}
        mock.calibration_errors = {3: int(ErrorCode.SPAN_OUT_OF_RANGE)}
        await anz.start_auto_calibration(confirm=True)
        wait = await anz.wait_for_calibration(timeout=5, interval=0.01)
    assert wait.saw_running
    assert wait.polls >= 2
    assert not wait.final.busy
    assert wait.failed
    assert wait.new_errors == {CH3: {ErrorCode.SPAN_OUT_OF_RANGE, ErrorCode.AUTO_CALIBRATION}}
    assert wait.final.calibration_error
    assert wait.elapsed_s > 0


async def test_an_error_left_from_before_still_counts_as_failed() -> None:
    mock = fast()
    mock.set_register("error.ch1.e4.active", 1)
    mock.set_register("status.calibration_error", 1)
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        result = await anz.start_auto_zero_calibration(confirm=True)
        assert result.before is not None
        wait = await anz.wait_for_calibration(timeout=5, interval=0.01, since=result.before)
        mock.calibration_errors = {2: int(ErrorCode.ZERO_AMOUNT_OVER_50)}
        again = await anz.start_auto_zero_calibration(confirm=True)
        second = await anz.wait_for_calibration(timeout=5, interval=0.01, since=again.before)
    assert wait.failed
    assert wait.new_errors == {}
    assert second.failed
    assert second.new_errors == {CH2: {ErrorCode.ZERO_AMOUNT_OVER_50}}


async def test_a_plan_with_an_undocumented_range_is_refused() -> None:
    mock = bench()
    mock.set_register("auto_calibration.ch1.range", 2)
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        with pytest.raises(FujiDecodeError, match="reads 2, which is not a range"):
            await anz.plan_auto_calibration()
        with pytest.raises(FujiDecodeError):
            await anz.start_auto_calibration(confirm=True)
    assert FC06 not in {r.request.function for r in mock.exchanges}


async def test_a_port_that_fails_in_the_status_read_breaks_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        stream = anz.session._port.transport.stream
        assert isinstance(stream, anyserial.SerialPort)
        cls = type(stream)
        original = cls.drain
        count = 0

        async def drain(self: anyserial.SerialPort) -> None:
            nonlocal count
            count += 1
            # The status read before and the command go out; the read after does not.
            if count > len(POLL) + 1:
                raise anyserial.SerialError("device reports an I/O error")
            await original(self)

        monkeypatch.setattr(cls, "drain", drain)
        result = await anz.return_to_measurement(confirm=True)
        assert anz.session.state is SessionState.BROKEN
    assert result.outcome is CommandOutcome.SENT
    assert isinstance(result.status_error, FujiConnectionError)


async def test_waiting_when_nothing_runs_returns_at_once() -> None:
    async with analyzer_on(bench()) as (anz, _):
        wait = await anz.wait_for_calibration(timeout=1)
    assert (wait.saw_running, wait.polls, wait.failed) == (False, 1, False)


async def test_a_wait_that_runs_out_says_what_it_saw() -> None:
    mock = fast(scale=1.0)  # 300 s phases: still running at the deadline
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        await anz.start_auto_zero_calibration(confirm=True)
        with pytest.raises(FujiTimeoutError, match="wait_for_calibration") as info:
            await anz.wait_for_calibration(timeout=0.2, interval=0.05)
        mock.finish_calibration()
    assert info.value.context.extra["saw_running"] is True
    assert info.value.context.extra["polls"] >= 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("inf")},
        {"timeout": True},
        {"timeout": "5"},
        {"timeout": 5, "interval": 0},
        {"timeout": 5, "interval": float("nan")},
    ],
)
async def test_wait_for_calibration_checks_its_arguments(kwargs: dict[str, Any]) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match="positive number of seconds"):
            await anz.wait_for_calibration(**kwargs)
    assert mock.exchanges == []


async def test_readings_during_a_calibration_are_not_valid() -> None:
    mock = fast(scale=1.0)
    mock.set_register("output_hold.enabled", 1)
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        await anz.start_auto_zero_calibration(confirm=True)
        frame = await anz.poll()
        source = PollSourceAdapter("zpa", anz)
        async with record(source, rate_hz=50.0, duration=0.1) as rec:
            rows = [sample_to_row(b["zpa"]) async for b in rec.stream]
        mock.finish_calibration()
        after = await anz.poll()
    assert {r.state for r in frame.readings} == {ReadingState.CALIBRATING}
    assert rows
    assert all(r["ch3_state"] == "calibrating" and r["ch3_valid"] is False for r in rows)
    assert all(r["auto_calibration_running"] is True for r in rows)
    assert {r.state for r in after.readings} == {ReadingState.OK}
    assert mock.transactions()[-len(POLL) :] == POLL


async def test_the_auto_calibration_range_is_measured_on_during_the_run() -> None:
    mock = fast(scale=1.0)
    mock.set_register("auto_calibration.ch3.range", 1)
    async with analyzer_on(mock, options=AUTO) as (anz, _):
        await anz.start_auto_zero_calibration(confirm=True)
        during = await anz.channel_status(CH3)
        mock.finish_calibration()
        after = await anz.channel_status(CH3)
    assert (during.range, after.range) == (2, 1)
    assert during.auto_zero_running
