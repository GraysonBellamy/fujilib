"""Manual calibration at the front panel: the tracker, plans, the facade and the simulated panel.

Design §6.5, protocol findings §14. The tracker's sequences follow the bench
analyzer's reads of 2026-09-29; the facade runs against the simulator, whose
operator presses one key per poll.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest

from fujilib.devices.decode import RegisterValue
from fujilib.devices.models import DisplayState
from fujilib.devices.operations import CalibrationStatus
from fujilib.devices.panel import (
    MANUAL_PLAN_SETTINGS,
    ManualCalibrationEvent,
    ManualCalibrationKind,
    ManualCalibrationOutcome,
    ManualCalibrationTracker,
    PanelObservation,
    plan_manual_calibration,
)
from fujilib.errors import (
    FujiCapabilityError,
    FujiDecodeError,
    FujiTimeoutError,
    FujiValidationError,
)
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.enums import (
    DisplayScreen,
    ErrorCode,
    KeyCode,
    ManualCalibrationResult,
    ManualCalibrationStep,
)
from fujilib.registry.registers import REGISTRY
from fujilib.sync import Fuji, SyncPortal
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockAnalyzer, MockRequest, mock_transport
from tests.conftest import approx
from tests.facade import FC04, analyzer_on, documented_only, when
from tests.factories import T0, frame, reading, status

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fujilib.devices.models import AdcValues, ChannelStatus, RangeInfo

pytestmark = pytest.mark.anyio

CH1, CH2, CH3, CH4, CH5 = (ChannelId.from_number(n) for n in range(1, 6))
ZERO, SPAN, ENT, ESC = KeyCode.ZERO, KeyCode.SPAN, KeyCode.ENT, KeyCode.ESC
UP, DOWN, MODE = KeyCode.UP, KeyCode.DOWN, KeyCode.MODE
COMPLETED = ManualCalibrationOutcome.COMPLETED
FAILED = ManualCalibrationOutcome.FAILED
CANCELLED = ManualCalibrationOutcome.CANCELLED
AMBIGUOUS = ManualCalibrationOutcome.AMBIGUOUS
FC03 = 0x03


# --- Observations --------------------------------------------------------------------------


def observe(
    second: float,
    step: int,
    *,
    result: int | None = 0,
    cursor: ChannelId | None = CH3,
    zero: Sequence[ChannelId] = (),
    span: Sequence[ChannelId] = (),
    errors: Mapping[ChannelId, Sequence[ErrorCode]] | None = None,
    o2: int = -12,
    adc: AdcValues | None = None,
    screen: int = DisplayScreen.MEASUREMENT,
) -> PanelObservation:
    """One read of the bench analyzer's panel, ``second`` seconds after ``T0``."""
    errors = errors or {}
    channels: dict[ChannelId, ChannelStatus] = {
        c: status(zero=c in zero, span=c in span, errors=tuple(errors.get(c, ())))
        for c in (CH1, CH2, CH3)
    }
    readings = {
        CH1: reading(CH1, Gas.CO2, -9, 2),
        CH2: reading(CH2, Gas.CO, -6, 3),
        CH3: reading(CH3, Gas.O2, o2, 2),
    }
    shown = ManualCalibrationStep(step) if step in set(ManualCalibrationStep) else step
    decoded: ManualCalibrationResult | int | None = result
    if result is not None and result in set(ManualCalibrationResult):
        decoded = ManualCalibrationResult(result)
    return PanelObservation(
        at=T0 + timedelta(seconds=second),
        display=DisplayState(
            screen=screen,
            calibration_step=shown,
            top_channel=CH1,
            cursor_channel=cursor,
            calibration_result=decoded,
            key=KeyCode(0),
        ),
        channels=MappingProxyType(channels),
        calibration_error=any(errors.values()),
        readings=MappingProxyType(readings),
        adc=adc,
    )


def run(*observations: PanelObservation) -> list[ManualCalibrationEvent]:
    """Feed ``observations`` in order; the events they end."""
    tracker = ManualCalibrationTracker()
    events = [tracker.feed(o) for o in observations]
    return [e for e in events if e is not None]


def one(*observations: PanelObservation) -> ManualCalibrationEvent:
    [event] = run(*observations)
    return event


# --- The tracker ---------------------------------------------------------------------------


def test_the_bench_zero_completes() -> None:
    # 2026-09-29 17:32:49-17:32:54: ZERO, DOWN, ENT, ENT (findings §14).
    event = one(
        observe(0.0, 0),
        observe(0.5, 4, cursor=CH1),
        observe(1.5, 4),
        observe(2.0, 5),
        observe(2.5, 5, zero=[CH3]),
        observe(3.0, 6, result=4, zero=[CH3]),
        observe(4.6, 0, result=6, o2=0),
    )
    assert event.kind is ManualCalibrationKind.ZERO
    assert event.outcome is COMPLETED
    assert event.ran
    assert event.channels == (CH3,)
    assert event.ranges == {CH3: 1}
    assert event.started_at == T0 + timedelta(seconds=0.5)
    assert event.selected_at == T0 + timedelta(seconds=2.0)
    assert event.ran_after == T0 + timedelta(seconds=2.5)
    assert event.ran_before == T0 + timedelta(seconds=3.0)
    assert event.calibrated_at == event.ran_before
    assert event.ended_at == T0 + timedelta(seconds=4.6)
    assert event.before[CH3].value == approx(-0.12)
    assert event.after[CH3].value == 0
    assert set(event.before) == set(event.after) == {CH3}
    assert event.new_errors == {}
    assert event.evidence == ("a read showed it running",)
    assert event.observations == 6


def test_the_readings_after_come_from_a_read_that_postdates_the_end() -> None:
    # The first read back on measurement read its readings and flags before the step.
    event = one(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 6, result=4, zero=[CH3]),
        observe(3.0, 0, result=6, zero=[CH3]),
        observe(4.0, 0, result=6, o2=0),
    )
    assert event.ended_at == T0 + timedelta(seconds=3.0)
    assert event.after[CH3].value == 0
    assert event.observations == 5


def test_a_new_pass_right_after_the_end_is_its_own() -> None:
    events = run(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 0, result=6, zero=[CH3]),
        observe(3.0, 7, result=6),
        observe(4.0, 0, result=6),
    )
    assert [(e.kind, e.outcome) for e in events] == [
        (ManualCalibrationKind.ZERO, COMPLETED),
        (ManualCalibrationKind.SPAN, CANCELLED),
    ]


def test_a_span_whose_run_falls_between_two_reads_is_settled_by_the_result_register() -> None:
    event = one(
        observe(0.0, 0, result=6),
        observe(1.0, 7, result=6),
        observe(2.0, 8, span=[CH3], o2=2069),
        observe(3.0, 0, result=6, o2=2095),
    )
    assert event.kind is ManualCalibrationKind.SPAN
    assert event.outcome is COMPLETED
    assert event.ran_after == T0 + timedelta(seconds=2.0)
    assert event.ran_before == T0 + timedelta(seconds=3.0)
    assert event.evidence == ("the result register went from 0 on the wait step to 4 or 6",)


def test_a_result_register_that_reads_running_on_the_wait_step_counts() -> None:
    event = one(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 5, result=4, zero=[CH3]),
        observe(3.0, 0, result=6),
    )
    assert event.outcome is COMPLETED
    assert event.ran_before == T0 + timedelta(seconds=2.0)


def test_the_bench_cancel_is_cancelled() -> None:
    # 17:37:39-17:37:44: ZERO, ENT, ESC.
    event = one(
        observe(0.0, 0, result=6),
        observe(1.0, 4, result=6),
        observe(5.0, 5, zero=[CH3], o2=2095),
        observe(5.5, 0, result=0, o2=2095),
    )
    assert event.outcome is CANCELLED
    assert not event.ran
    assert event.calibrated_at is None
    assert (event.ran_after, event.ran_before) == (None, None)
    assert event.evidence == ("it reached the wait step, and the result register still reads 0",)


def test_leaving_channel_selection_is_cancelled_whatever_the_result_register_says() -> None:
    event = one(observe(0.0, 4, result=6), observe(1.0, 0, result=6))
    assert event.outcome is CANCELLED
    assert event.evidence == (
        "no zero or span flag was seen set; the channel is the cursor's",
        "it never reached the wait step, so no gas was selected",
    )
    assert event.channels == (CH3,)
    assert event.selected_at is None


def test_without_the_result_register_a_missed_run_is_ambiguous() -> None:
    event = one(
        observe(0.0, 4, result=None),
        observe(1.0, 5, result=None, zero=[CH3]),
        observe(2.0, 0, result=None, o2=0),
    )
    assert event.outcome is AMBIGUOUS
    assert event.evidence[-1] == (
        "it reached the wait step, no read showed it running, and the result register "
        "(not read) does not say whether it ran"
    )


def test_a_stale_result_register_does_not_count() -> None:
    # The wait step was never read with 0, so a 6 at the end may be the last calibration's.
    event = one(observe(0.0, 5, result=6, zero=[CH3]), observe(1.0, 0, result=6))
    assert event.outcome is AMBIGUOUS
    assert event.evidence[0] == "first seen on step zero_wait, not on channel selection"
    assert "result register (completed)" in event.evidence[-1]


def test_an_undocumented_result_value_is_ambiguous() -> None:
    event = one(observe(0.0, 4), observe(1.0, 5, zero=[CH3]), observe(2.0, 0, result=9))
    assert event.outcome is AMBIGUOUS
    assert "result register (9)" in event.evidence[-1]


def test_the_error_display_is_a_failure() -> None:
    event = one(
        observe(0.0, 0),
        observe(1.0, 4),
        observe(2.0, 5, zero=[CH3]),
        observe(3.0, 10, result=4, zero=[CH3], errors={CH3: [ErrorCode.ZERO_AMOUNT_OVER_50]}),
        observe(4.0, 0, result=4, errors={CH3: [ErrorCode.ZERO_AMOUNT_OVER_50]}),
    )
    assert event.outcome is FAILED
    assert event.ran
    assert event.new_errors == {CH3: {ErrorCode.ZERO_AMOUNT_OVER_50}}
    assert event.ran_before == T0 + timedelta(seconds=3.0)
    assert event.evidence == (
        "the error display was shown",
        "calibration error(s) 5 appeared on its channels",
    )


def test_a_new_calibration_error_is_a_failure_even_unseen() -> None:
    event = one(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 0, result=0, errors={CH3: [ErrorCode.UNSTABLE]}),
    )
    assert event.outcome is FAILED
    assert event.calibrated_at == event.ended_at
    assert event.evidence == ("calibration error(s) 8 appeared on its channels",)


def test_an_error_left_from_before_is_not_new() -> None:
    old = {CH3: [ErrorCode.SPAN_OUT_OF_RANGE]}
    event = one(
        observe(0.0, 0, errors=old),
        observe(1.0, 4, errors=old),
        observe(2.0, 5, zero=[CH3], errors=old),
        observe(3.0, 6, result=4, zero=[CH3], errors=old),
        observe(4.0, 0, result=6, errors=old),
    )
    assert event.outcome is COMPLETED
    assert event.new_errors == {}


def test_an_error_on_another_channel_is_not_its_own() -> None:
    event = one(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 0, result=6, errors={CH1: [ErrorCode.ZERO_OUT_OF_RANGE]}),
    )
    assert event.outcome is COMPLETED
    assert event.new_errors == {}


def test_an_at_once_zero_calibrates_every_flagged_channel() -> None:
    event = one(
        observe(0.0, 4, cursor=CH1),
        observe(1.0, 5, cursor=CH1, zero=[CH1, CH2]),
        observe(2.0, 6, result=4, cursor=CH1, zero=[CH1, CH2]),
        observe(3.0, 0, result=6, cursor=CH1),
    )
    assert event.channels == (CH1, CH2)
    assert set(event.before) == {CH1, CH2}
    assert event.ranges == {CH1: 1, CH2: 1}


@pytest.mark.parametrize("kind", [ManualCalibrationKind.ZERO, ManualCalibrationKind.SPAN])
def test_a_pass_first_seen_on_the_error_display_takes_its_kind_from_the_flags(
    kind: ManualCalibrationKind,
) -> None:
    zero = [CH3] if kind is ManualCalibrationKind.ZERO else []
    span = [CH3] if kind is ManualCalibrationKind.SPAN else []
    error = {CH3: [ErrorCode.SPAN_OUT_OF_RANGE]}
    event = one(
        observe(0.0, 10, result=4, errors=error, zero=zero, span=span),
        observe(1.0, 0, result=4, errors={CH3: [ErrorCode.SPAN_OUT_OF_RANGE]}),
    )
    assert event.kind is kind
    assert event.outcome is FAILED
    assert event.new_errors == {}  # already active at the first read
    assert event.evidence[0] == "first seen on step error_display, not on channel selection"


def test_a_pass_with_no_kind_at_all_is_taken_as_a_zero() -> None:
    event = one(observe(0.0, 10, result=4, cursor=None), observe(1.0, 0, cursor=None))
    assert event.kind is ManualCalibrationKind.ZERO
    assert event.channels == ()
    assert event.ranges == {}
    assert event.evidence[-1] == (
        "neither a zero nor a span step or flag was seen; taken as a zero"
    )


def test_a_new_pass_that_starts_between_two_reads_ends_the_last() -> None:
    events = run(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 6, result=4, zero=[CH3]),
        observe(3.0, 7, result=6),
        observe(4.0, 8, span=[CH3]),
        observe(5.0, 0, result=6),
    )
    assert [e.kind for e in events] == [ManualCalibrationKind.ZERO, ManualCalibrationKind.SPAN]
    assert events[0].outcome is COMPLETED
    assert events[0].evidence[-1] == (
        "the next pass began before a read showed the measurement screen"
    )
    assert events[1].outcome is COMPLETED


def test_odd_reads_are_kept_as_evidence() -> None:
    event = one(
        observe(0.0, 4),
        observe(1.0, 3),
        observe(2.0, 5, zero=[CH3]),
        observe(3.0, 6, result=4, zero=[CH3]),
        observe(4.0, 0, result=0),
    )
    assert event.outcome is COMPLETED
    assert "an undocumented step 3 was shown" in event.evidence
    assert "the result register reads 0 although a read showed it running" in event.evidence


def test_the_tracker_says_where_it_is() -> None:
    tracker = ManualCalibrationTracker()
    assert (tracker.active, tracker.step) == (False, None)
    assert tracker.feed(observe(0.0, 0)) is None
    assert tracker.feed(observe(1.0, 4)) is None
    assert (tracker.active, tracker.step) == (True, ManualCalibrationStep.ZERO_CHANNEL_SELECT)
    assert tracker.feed(observe(2.0, 0)) is not None
    assert tracker.active is False


def test_menu_pages_are_not_steps() -> None:
    # 2026-09-29 18:06-18:15: the menus put their page numbers in 30182, 4, 5 and 8
    # among them, and nothing was calibrated (findings §15).
    pages = [
        (DisplayScreen.PARAMETER_SETTING, 1),
        (DisplayScreen.MAINTENANCE, 1),  # sensor input
        (DisplayScreen.MAINTENANCE, 22),  # calibration log
        (DisplayScreen.MAINTENANCE, 5),  # the factory password
        (DisplayScreen.FACTORY, 26),  # A/D data
        (DisplayScreen.FACTORY, 4),
        (DisplayScreen.FACTORY, 8),
        (DisplayScreen.MEASUREMENT, 0),
    ]
    tracker = ManualCalibrationTracker()
    for second, (screen, page) in enumerate(pages):
        assert tracker.feed(observe(float(second), page, result=6, screen=screen)) is None
        assert (tracker.active, tracker.step) == (False, ManualCalibrationStep.NONE)


@pytest.mark.parametrize(
    ("screen", "flags_still_set", "shown"),
    [
        (DisplayScreen.MAINTENANCE, False, "the maintenance screen"),
        (DisplayScreen.MAINTENANCE, True, "the maintenance screen"),
        (11, False, "undocumented screen 11"),
    ],
)
def test_a_pass_left_for_a_menu_ends_there(screen: int, flags_still_set: bool, shown: str) -> None:
    # ESC and MODE between two reads, then a menu page numbered like the zero wait step.
    zero = [CH3] if flags_still_set else []
    events = run(
        observe(0.0, 4),
        observe(1.0, 5, zero=[CH3]),
        observe(2.0, 5, zero=zero, screen=screen),
        observe(3.0, 0),
    )
    [event] = events
    assert event.outcome is CANCELLED
    assert event.ended_at == T0 + timedelta(seconds=2.0)
    assert event.evidence == (
        "it reached the wait step, and the result register still reads 0",
        f"the first read after it showed {shown}",
    )


def test_deviations_need_a_reading_and_a_gas() -> None:
    event = one(
        observe(0.0, 7),
        observe(1.0, 8, span=[CH3], o2=2069),
        observe(2.0, 0, result=6, o2=2095),
    )
    assert event.deviations == {}
    with_gas = replace(event, gases=MappingProxyType({CH3: 20.95, CH1: None}))
    assert with_gas.deviations == {CH3: approx(-0.26)}
    no_reading = replace(with_gas, before=MappingProxyType({}))
    assert no_reading.deviations == {}


def test_observations_from_frames_and_statuses() -> None:
    assert PanelObservation.from_frame(frame(detail=False)) is None
    seen = PanelObservation.from_frame(frame())
    assert seen is not None
    assert seen.step is ManualCalibrationStep.NONE
    assert set(seen.readings) == {CH1, CH2, CH3}
    assert set(seen.channels) == {CH1, CH2, CH3}
    status_block = frame().analyzer
    assert status_block is not None
    blind = replace(frame(), analyzer=replace(status_block, display=None))
    assert PanelObservation.from_frame(blind) is None
    calibration = CalibrationStatus(
        running=False,
        channels=MappingProxyType({CH3: status(zero=True)}),
        calibration_error=False,
        display=status_block.display,
        read_at=T0,
    )
    from_status = PanelObservation.from_status(calibration)
    assert from_status is not None
    assert (from_status.at, from_status.readings) == (T0, {})
    assert PanelObservation.from_status(replace(calibration, display=None)) is None
    no_status_timing = replace(frame(), status_timing=None)
    fallback = PanelObservation.from_frame(no_status_timing)
    assert fallback is not None
    assert fallback.at == no_status_timing.readings_timing.received_at


# --- Plans ---------------------------------------------------------------------------------


def settings(**raw: int) -> dict[str, RegisterValue]:
    """The plan's settings as the bench unit has them, with ``raw`` (dots as ``__``) changed."""
    values: dict[str, RegisterValue] = {}
    bench = {
        "calibration.ch1.zero_mode": 1,
        "calibration.ch2.zero_mode": 1,
        "calibration_gas.ch3.range1.span": 2095,
        "calibration_gas.ch3.range2.span": 2001,
    }
    changed = {k.replace("__", "."): v for k, v in raw.items()}
    for name in MANUAL_PLAN_SETTINGS:
        word = changed.get(name, bench.get(name, 0))
        spec = REGISTRY.resolve(name)
        scaled = float(word) / 100 if name.startswith("calibration_gas") else word
        values[name] = RegisterValue(spec, (word,), word, scaled, "vol%" if spec.unit else None)
    return values


RANGES: tuple[RangeInfo, ...] = ()


def test_a_zero_of_an_each_channel_plans_that_channel_on_its_range() -> None:
    plan = plan_manual_calibration(
        settings(), ManualCalibrationKind.ZERO, CH3, ranges=RANGES, established=[CH1, CH2, CH3]
    )
    assert plan.channels == (CH3,)
    [target] = plan.targets
    assert (target.ranges, target.span, target.widened, target.established) == (
        (1,),
        False,
        False,
        True,
    )
    assert (target.zero_gas, target.span_gas) == ((0.0,), (20.95,))
    assert plan.notes == ()


def test_a_zero_of_an_at_once_channel_zeroes_all_of_them() -> None:
    plan = plan_manual_calibration(
        settings(), ManualCalibrationKind.ZERO, CH2, ranges=RANGES, established=[CH1, CH2, CH3]
    )
    assert plan.channels == (CH1, CH2)
    assert plan.notes == ("CH2 is set to zero 'at once' with CH1: they are zeroed together",)


def test_a_span_of_an_at_once_channel_is_that_channel_only() -> None:
    plan = plan_manual_calibration(
        settings(), ManualCalibrationKind.SPAN, CH1, ranges=RANGES, established=[CH1, CH2, CH3]
    )
    assert plan.channels == (CH1,)
    assert plan.targets[0].span


def test_a_lone_at_once_channel_needs_no_note() -> None:
    plan = plan_manual_calibration(
        settings(calibration__ch2__zero_mode=0),
        ManualCalibrationKind.ZERO,
        CH1,
        ranges=RANGES,
        established=[CH1],
    )
    assert (plan.channels, plan.notes) == ((CH1,), ())


def test_both_ranges_auto_range_and_absent_channels() -> None:
    plan = plan_manual_calibration(
        settings(
            calibration__ch3__range_mode=1,
            calibration__ch4__zero_mode=1,
            range__ch4__method=2,
            auto_calibration__ch4__range=1,
        ),
        ManualCalibrationKind.ZERO,
        CH4,
        ranges=RANGES,
        established=[CH1, CH2, CH3],
    )
    assert plan.channels == (CH1, CH2, CH4)
    by_channel = {t.channel: t for t in plan.targets}
    assert by_channel[CH4].ranges == (2,)
    assert not by_channel[CH4].established
    both = plan_manual_calibration(
        settings(calibration__ch3__range_mode=1),
        ManualCalibrationKind.SPAN,
        CH3,
        ranges=RANGES,
        established=[CH3],
    )
    assert (both.targets[0].ranges, both.targets[0].widened) == ((1, 2), True)
    assert both.targets[0].span_gas == (20.95, 20.01)
    assert plan.notes[-2:] == (
        "CH4 switches range automatically, so it is calibrated on its auto-calibration "
        "range, range 2 (ZPA manual p.76)",
        "CH4 would be calibrated but is not established: whether it is fitted is not known",
    )


@pytest.mark.parametrize(
    "change",
    [{"range__ch3__current": 2}, {"range__ch3__method": 2, "auto_calibration__ch3__range": 5}],
)
def test_a_range_that_is_not_a_range_is_refused(change: dict[str, int]) -> None:
    with pytest.raises(FujiDecodeError, match="which is not a range"):
        plan_manual_calibration(
            settings(**change), ManualCalibrationKind.ZERO, CH3, ranges=RANGES, established=[]
        )


def test_only_measured_channels_are_planned() -> None:
    with pytest.raises(FujiValidationError, match="not a measured channel"):
        plan_manual_calibration(
            settings(), ManualCalibrationKind.ZERO, ChannelId.CH6, ranges=RANGES, established=[]
        )


def test_an_unscaled_gas_is_unknown() -> None:
    values = settings()
    spec = REGISTRY.resolve("calibration_gas.ch3.range1.zero")
    values["calibration_gas.ch3.range1.zero"] = RegisterValue(spec, (0,), 0, None, None)
    plan = plan_manual_calibration(
        values, ManualCalibrationKind.ZERO, CH3, ranges=RANGES, established=[CH3]
    )
    assert (plan.targets[0].zero_gas, plan.targets[0].units) == ((None,), ("?",))


# --- The facade, on the simulator ----------------------------------------------------------


def operator(mock: MockAnalyzer, keys: Sequence[int | None]) -> None:
    """Press ``keys`` one per poll, at its first block; ``None`` presses nothing."""
    pending = list(keys)

    def on_request(request: MockRequest) -> None:
        if pending and request.function == FC04 and request.address == 0 and request.count == 61:
            key = pending.pop(0)
            if key is not None:
                mock.press(key, at=request.arrived_at)

    mock.on_request = on_request


def panel(**config: Any) -> MockAnalyzer:
    """The bench analyzer, its panel offering channels 1-3."""
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, panel_channels=(1, 2, 3), **config))


async def test_watching_a_zero_made_at_the_panel() -> None:
    mock = panel(manual_calibration_s=0.05)
    mock.set_reading("CH3", -12, 2)
    operator(mock, [None, ZERO, DOWN, ENT, ENT])
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01, adc=True)
    assert event.outcome is COMPLETED
    assert event.kind is ManualCalibrationKind.ZERO
    assert event.channels == (CH3,)
    assert event.evidence == ("a read showed it running",)
    assert event.before[CH3].value == approx(-0.12)
    assert event.after[CH3].value == 0
    assert event.gases == {CH3: 0.0}
    assert event.deviations == {CH3: approx(-0.12)}
    assert event.adc_before is not None
    assert len(event.adc_before.raw) == 21
    assert [k for k, _ in mock.keys] == [ZERO, DOWN, ENT, ENT]


async def test_a_run_between_two_polls_is_settled_by_the_result_register() -> None:
    mock = panel()
    operator(mock, [SPAN, DOWN, DOWN, ENT, ENT])
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    assert event.outcome is COMPLETED
    assert event.kind is ManualCalibrationKind.SPAN
    assert event.evidence == ("the result register went from 0 on the wait step to 4 or 6",)
    assert event.after[CH3].value == approx(20.95)
    assert event.gases == {CH3: approx(20.95)}
    assert event.adc_before is None


async def test_watching_a_cancel() -> None:
    mock = panel()
    operator(mock, [ZERO, DOWN, ENT, ESC])
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    assert event.outcome is CANCELLED
    assert event.gases == {CH3: 0.0}
    assert event.deviations == {}  # nothing was calibrated, so nothing deviated


async def test_menu_pages_before_a_zero_are_not_taken_for_one() -> None:
    # Pages the bench unit's menus showed (findings §15), one per poll, then a zero.
    mock = panel(manual_calibration_s=0.05)
    pages = [
        (DisplayScreen.MAINTENANCE, 5),
        (DisplayScreen.FACTORY, 4),
        (DisplayScreen.FACTORY, 8),
        (DisplayScreen.MEASUREMENT, 0),
    ]
    keys = [ZERO, DOWN, ENT, ENT]

    def on_request(request: MockRequest) -> None:
        if request.function == FC04 and request.address == 0 and request.count == 61:
            if pages:
                screen, page = pages.pop(0)
                mock.set_register("display.screen", int(screen))
                mock.set_register("display.calibration_step", page)
            elif keys:
                mock.press(keys.pop(0), at=request.arrived_at)

    mock.on_request = on_request
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    assert (event.kind, event.outcome, event.channels) == (
        ManualCalibrationKind.ZERO,
        COMPLETED,
        (CH3,),
    )
    assert event.evidence == ("a read showed it running",)


async def test_watching_a_failure_and_its_error_display() -> None:
    mock = panel(manual_calibration_s=0.02)
    mock.calibration_errors = {3: int(ErrorCode.SPAN_OUT_OF_RANGE)}
    operator(mock, [SPAN, DOWN, DOWN, ENT, ENT, None, None, None, ESC])
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    assert event.outcome is FAILED
    assert event.new_errors == {CH3: {ErrorCode.SPAN_OUT_OF_RANGE}}
    assert event.evidence[:2] == (
        "the error display was shown",
        "calibration error(s) 6 appeared on its channels",
    )


async def test_the_gases_that_cannot_be_read_are_left_out() -> None:
    mock = panel()
    operator(mock, [ZERO, DOWN, ENT, ENT])
    mock.inject(FaultKind.EXCEPTION, exception_code=0x04, when=when(FC03, 0x0008))
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    assert event.outcome is COMPLETED
    assert event.gases == {}
    assert event.evidence[-1].startswith("the calibration gases could not be read")


async def test_an_event_without_a_range_reads_no_gas() -> None:
    mock = panel()
    mock.set_register("range.ch3.current", 5)
    operator(mock, [ZERO, DOWN, ENT, ENT])
    async with analyzer_on(mock) as (anz, _):
        event = await anz.wait_for_manual_calibration(timeout=5, interval=0.01)
    assert event.ranges == {CH3: 6}
    assert event.gases == {}
    assert FC03 not in {r.request.function for r in mock.exchanges}


async def test_a_wait_that_runs_out_says_whether_a_pass_was_under_way() -> None:
    mock = panel()
    operator(mock, [ZERO])
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiTimeoutError, match="wait_for_manual_calibration") as info:
            await anz.wait_for_manual_calibration(timeout=0.3, interval=0.02)
    assert info.value.context.extra["in_progress"] is True
    assert info.value.context.extra["polls"] >= 2


@pytest.mark.parametrize(
    "kwargs", [{"timeout": 0}, {"timeout": "5"}, {"timeout": 5, "interval": float("inf")}]
)
async def test_the_wait_checks_its_arguments(kwargs: dict[str, Any]) -> None:
    mock = panel()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match="positive number of seconds"):
            await anz.wait_for_manual_calibration(**kwargs)
    assert mock.exchanges == []


async def test_asking_for_the_a_d_values_where_there_are_none_sends_nothing() -> None:
    mock = MockAnalyzer(documented_only())
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiCapabilityError, match="adc_values"):
            await anz.wait_for_manual_calibration(timeout=5, adc=True)
    assert mock.exchanges == []


async def test_planning_a_manual_calibration_on_the_bench_analyzer() -> None:
    mock = panel()
    async with analyzer_on(mock) as (anz, _):
        zero = await anz.plan_manual_calibration("CH1", "zero")
        span = await anz.plan_manual_calibration(CH3, ManualCalibrationKind.SPAN)
    # The bench unit also has the absent Ch4 and Ch5 set to "at once".
    assert zero.channels == (CH1, CH2, CH4, CH5)
    assert [t.established for t in zero.targets] == [True, True, False, False]
    assert zero.notes[-1] == (
        "CH4, CH5 would be calibrated but is not established: whether it is fitted is not known"
    )
    assert zero.targets[0].units == ("vol%",)
    assert span.channels == (CH3,)
    assert span.targets[0].span_gas[0] == approx(20.95)


@pytest.mark.parametrize(("channel", "kind"), [("CH6", "zero"), ("CH3", "both"), ("CH3", 1)])
async def test_a_bad_plan_request_sends_nothing(channel: str, kind: Any) -> None:
    mock = panel()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError):
            await anz.plan_manual_calibration(channel, kind)
    assert mock.exchanges == []


def test_the_blocking_twins() -> None:
    mock = panel()
    operator(mock, [ZERO, DOWN, ENT, ENT])
    with (
        SyncPortal() as portal,
        portal.wrap_async_context_manager(mock_transport(mock)) as (transport, _line),
        Fuji.open(transport, channel_map={"CH3": "o2"}, portal=portal) as anz,
    ):
        assert anz.plan_manual_calibration("CH3", "zero").channels == (CH3,)
        event = anz.wait_for_manual_calibration(timeout=10, interval=0.01)
    assert event.outcome is COMPLETED


# --- The simulated panel -------------------------------------------------------------------


def shown(mock: MockAnalyzer) -> tuple[int, int, int, int]:
    """Step, cursor channel, result and key as the registers hold them."""
    return (
        mock.register("display.calibration_step")[0],
        mock.register("display.cursor_channel")[0] + 1,
        mock.register("display.calibration_result")[0],
        mock.register("display.key")[0],
    )


def flags(mock: MockAnalyzer, kind: str) -> tuple[int, ...]:
    return tuple(mock.register(f"status.ch{c}.{kind}_calibrating")[0] for c in range(1, 6))


def test_the_cursor_wraps_round_and_at_once_channels_share_one_position() -> None:
    # Protocol findings §18.2: the bench unit's cursor wraps at both ends, and the
    # "at once" pair's position reads Ch1 reached going down and Ch2 going up.
    mock = panel()
    mock.press(ZERO, at=0.0)
    assert shown(mock) == (4, 1, 0, ZERO)
    mock.press(DOWN, at=1.0)
    assert shown(mock)[:2] == (4, 3)  # Ch1 and Ch2 are one position for a zero
    mock.press(DOWN, at=2.0)
    assert shown(mock)[:2] == (4, 1)  # round to the pair, going down
    mock.press(UP, at=3.0)
    assert shown(mock)[:2] == (4, 3)  # round to the last position
    mock.press(UP, at=4.0)
    assert shown(mock)[:2] == (4, 2)  # the pair, going up
    mock.press(ENT, at=5.0)
    assert flags(mock, "zero") == (1, 1, 0, 0, 0)
    mock.press(ESC, at=6.0)
    assert shown(mock)[0] == 0
    assert flags(mock, "zero") == (0,) * 5
    mock.press(SPAN, at=7.0)
    assert shown(mock)[:2] == (7, 2)  # it opens where the cursor was
    mock.press(DOWN, at=8.0)
    assert shown(mock)[:2] == (7, 3)  # a span has no shared position


def test_esc_leaves_channel_selection_and_other_keys_wait() -> None:
    mock = panel()
    mock.press(ZERO, at=0.0)
    mock.press(SPAN, at=0.5)  # channel selection ignores it
    assert shown(mock)[0] == 4
    mock.press(ESC, at=1.0)
    assert shown(mock)[0] == 0
    for t, key in enumerate((ZERO, DOWN, ENT, UP, MODE), start=2):
        mock.press(key, at=float(t))
    assert shown(mock)[:3] == (5, 3, 0)  # the wait step ignores keys but ENT and ESC
    assert mock.register("display.screen") == (0,)


def test_esc_clears_an_error_display_it_did_not_start() -> None:
    mock = panel()
    mock.set_register("display.calibration_step", 10)
    mock.press(ESC, at=0.0)
    assert shown(mock)[0] == 0


def test_the_key_register_shows_a_key_for_a_while() -> None:
    mock = panel(key_hold_s=0.3)
    mock.press(KeyCode.SIDE, at=0.0)
    assert shown(mock)[3] == KeyCode.SIDE
    mock.press(UP, at=1.0)  # on measurement: shown, nothing else
    assert shown(mock) == (0, 1, 0, UP)
    assert mock.keys == [(KeyCode.SIDE, 0.0), (UP, 1.0)]


async def test_the_key_register_clears_by_itself() -> None:
    mock = panel(key_hold_s=0.0)
    mock.press(ZERO)
    async with analyzer_on(mock) as (anz, _):
        state = await anz.status()
    assert state.display is not None
    assert state.display.key == KeyCode(0)
    assert state.display.calibration_step is ManualCalibrationStep.ZERO_CHANNEL_SELECT


def test_a_calibration_sets_its_reading_to_the_gas_of_its_range() -> None:
    mock = panel()
    mock.set_register("range.ch3.current", 1)  # range 2, whose span gas is 20.01
    for t, key in enumerate((SPAN, DOWN, DOWN, ENT, ENT)):
        mock.press(key, at=float(t))
    assert shown(mock)[:3] == (0, 3, 6)
    assert mock.register("reading.ch3.value") == (2001,)
    assert flags(mock, "span") == (0,) * 5


def test_output_hold_holds_the_channels_being_calibrated() -> None:
    mock = panel()
    mock.set_register("output_hold.enabled", 1)
    for t, key in enumerate((ZERO, DOWN, ENT)):
        mock.press(key, at=float(t))
    assert mock.register("status.ch3.hold") == (1,)
    mock.press(ESC, at=3.0)
    assert mock.register("status.ch3.hold") == (0,)


def test_keys_while_it_runs_and_in_other_screens_are_ignored() -> None:
    mock = panel(manual_calibration_s=10.0)
    for t, key in enumerate((ZERO, DOWN, ENT, ENT, ESC)):
        mock.press(key, at=float(t))
    assert shown(mock)[:3] == (6, 3, 4)
    mock.press(ENT, at=20.0)  # the run ended at 13 s; ENT on measurement does nothing
    assert shown(mock)[:3] == (0, 3, 6)
    mock.set_register("display.screen", int(DisplayScreen.PARAMETER_SETTING))
    mock.press(ZERO, at=21.0)
    assert shown(mock)[0] == 0


def test_mode_opens_the_menu_and_esc_closes_it() -> None:
    mock = panel()
    mock.press(MODE, at=0.0)
    assert mock.register("display.screen") == (DisplayScreen.MENU,)
    mock.press(ZERO, at=1.0)
    assert mock.register("display.screen") == (DisplayScreen.MENU,)
    mock.press(ESC, at=2.0)
    assert mock.register("display.screen") == (0,)


@pytest.mark.parametrize(
    ("error", "key", "step", "result", "reading"),
    [
        (ErrorCode.ZERO_AMOUNT_OVER_50, ENT, 0, 6, 0),  # forced: carried out to the end
        (ErrorCode.ZERO_AMOUNT_OVER_50, ESC, 0, 4, 2018),  # stopped
        (ErrorCode.ZERO_OUT_OF_RANGE, ENT, 0, 4, 2018),  # not forceable: the display clears
        (ErrorCode.ZERO_AMOUNT_OVER_50, UP, 10, 4, 2018),  # other keys do nothing
    ],
)
def test_the_error_display(
    error: ErrorCode, key: KeyCode, step: int, result: int, reading: int
) -> None:
    mock = panel()
    mock.calibration_errors = {3: int(error), 1: int(ErrorCode.UNSTABLE)}
    for t, pressed in enumerate((ZERO, DOWN, ENT, ENT)):
        mock.press(pressed, at=float(t))
    assert shown(mock)[:3] == (10, 3, 4)
    assert mock.register(f"error.ch3.e{int(error)}.active") == (1,)
    assert mock.register("status.calibration_error") == (1,)
    assert mock.calibration_errors == {1: int(ErrorCode.UNSTABLE)}  # Ch1 was not calibrated
    mock.press(key, at=10.0)
    assert shown(mock)[:3] == (step, 3, result)
    assert mock.register("reading.ch3.value") == (reading,)
    if step == 0:
        assert flags(mock, "zero") == (0,) * 5


# --- The simulated panel, keyed over Modbus (protocol findings §18) --------------------------


def modbus(mock: MockAnalyzer, address: int, value: int, at: float) -> Any:
    """Write ``value`` to ``address`` with FC06 at AnyIO time ``at``; the reply's fault."""
    from fujilib.testing import MockExchange

    request = MockRequest(1, 0x06, address, 1, (value,), b"", at)
    return mock.answer(MockExchange(request))[1]


def read_at(mock: MockAnalyzer, at: float) -> Any:
    """A status read at AnyIO time ``at``; the reply's fault."""
    from fujilib.testing import MockExchange

    request = MockRequest(1, FC04, 0x0083, 60, (), b"", at)
    return mock.answer(MockExchange(request))[1]


def test_a_key_written_over_modbus_acts_but_never_shows_in_30190() -> None:
    mock = panel()
    mock.set_register("display.key", 0)
    for t, key in enumerate((ZERO, DOWN, ENT)):
        assert modbus(mock, 0x07D0, key, float(t)) is None
    assert shown(mock) == (5, 3, 0, 0)
    assert flags(mock, "zero") == (0, 0, 1, 0, 0)
    assert [k for k, _ in mock.remote_keys] == [ZERO, DOWN, ENT]
    assert mock.keys == []  # nothing pressed at the panel


def test_key_lock_swallows_a_key_over_modbus_and_the_analyzer_falls_silent() -> None:
    mock = panel(key_lock_silence_s=1.6)
    mock.set_register("key_lock", 1)
    assert modbus(mock, 0x07D0, ZERO, 10.0) is None  # acknowledged
    assert shown(mock)[0] == 0
    assert [k for k, _ in mock.swallowed_keys] == [ZERO]
    assert read_at(mock, 11.0).kind is FaultKind.DROP  # nothing answers for 1.6 s
    assert read_at(mock, 11.7) is None
    assert not mock.silent(12.0)
    mock.press(ZERO, at=13.0)  # key lock is not modelled at the panel
    assert shown(mock)[0] == 4


def test_zero_opens_on_the_first_position_after_42002_and_after_a_long_pause() -> None:
    mock = panel(cursor_reset_idle_s=60.0)
    mock.set_register("display.cursor_channel", 2)
    modbus(mock, 0x07D1, 1, 0.0)  # return to measurement
    mock.press(ZERO, at=1.0)
    assert shown(mock)[:2] == (4, 1)
    mock.press(DOWN, at=2.0)
    mock.press(ESC, at=3.0)
    mock.press(ZERO, at=4.0)
    assert shown(mock)[:2] == (4, 3)  # where it was left
    mock.press(ESC, at=5.0)
    mock.press(SPAN, at=100.0)
    assert shown(mock)[:2] == (7, 1)  # after a long pause


def test_a_flag_can_clear_a_moment_after_esc_leaves_the_wait_step() -> None:
    mock = panel(flag_lag_s=0.2)
    for t, key in enumerate((SPAN, DOWN, DOWN, ENT, ESC)):
        mock.press(key, at=float(t))
    assert shown(mock)[0] == 0
    assert flags(mock, "span") == (0, 0, 1, 0, 0)  # still set just after the step
    read_at(mock, 4.3)
    assert flags(mock, "span") == (0,) * 5


def test_the_analyzer_falls_silent_while_it_stores_a_calibration() -> None:
    mock = panel(manual_calibration_s=2.0, storing_silence_s=1.0)
    for t, key in enumerate((ZERO, DOWN, ENT, ENT)):
        mock.press(key, at=float(t))
    assert read_at(mock, 3.5) is None  # running, answering
    assert read_at(mock, 4.5).kind is FaultKind.DROP  # storing
    assert read_at(mock, 5.1) is None
    assert shown(mock)[:3] == (0, 3, 6)


def test_a_gas_at_the_inlet_approaches_its_value() -> None:
    mock = panel()
    mock.set_reading("CH3", 2095, 2)
    mock.flow("CH3", 0.0, tau_s=10.0, at=0.0)
    read_at(mock, 10.0)
    assert mock.register("reading.ch3.value") == (round(2095 * 0.36788),)  # e**-1
    mock.flow("CH1", 1.5, at=10.0)
    assert mock.register("reading.ch1.value") == (150,)
    for t, key in enumerate((ZERO, DOWN, ENT, ENT), start=11):
        mock.press(key, at=float(t))
    read_at(mock, 20.0)
    assert mock.register("reading.ch3.value") == (0,)  # the calibration set it; no more flow
    read_at(mock, 100.0)
    assert mock.register("reading.ch3.value") == (0,)
