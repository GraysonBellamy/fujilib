"""The steadiness rule a remote calibration waits on (design §13.1 #78).

Pure: reads are fed with a monotonic time; nothing is sent.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fujilib.devices.steadiness import SteadinessJudge, SteadinessRule, SteadinessTarget
from fujilib.errors import FujiValidationError
from fujilib.registry.channels import ChannelId
from tests.conftest import approx

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

CH1, CH2, CH3 = ChannelId.CH1, ChannelId.CH2, ChannelId.CH3
O2 = SteadinessTarget(gas=20.95, full_scale=21.0, unit="vol%", response_time_s=15)


def last(
    j: SteadinessJudge, reads: Iterable[tuple[float, Mapping[ChannelId, float | None]]]
) -> Any:
    """Feed ``reads`` in order; the last verdict."""
    verdicts = [j.feed(t, r) for t, r in reads]
    return verdicts[-1]


def judge(rule: SteadinessRule | None = None, **targets: SteadinessTarget) -> SteadinessJudge:
    chosen = {ChannelId(k): v for k, v in targets.items()} or {CH3: O2}
    return SteadinessJudge(rule or SteadinessRule(), chosen)


def test_the_defaults_are_the_designs() -> None:
    rule = SteadinessRule()
    assert (rule.window_s, rule.response_factor, rule.band_percent_fs) == (30.0, 2.0, 0.5)
    assert (rule.tolerance_percent_fs, rule.timeout_s, rule.max_gap_s) == (10.0, 600.0, 5.0)


def test_the_window_is_the_longer_of_its_floor_and_twice_the_response_time() -> None:
    rule = SteadinessRule()
    assert rule.window_for(None) == 30.0
    assert rule.window_for(10) == 30.0
    assert rule.window_for(15) == 30.0
    assert rule.window_for(60) == 120.0
    assert judge().window(CH3) == 30.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_s": 0},
        {"window_s": -1},
        {"band_percent_fs": math.nan},
        {"tolerance_percent_fs": math.inf},
        {"timeout_s": True},
        {"timeout_s": "600"},
        {"response_factor": -0.5},
        {"response_factor": math.nan},
        {"max_gap_s": 0},
    ],
)
def test_a_rule_that_cannot_be_met_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(FujiValidationError, match="must be a"):
        SteadinessRule(**kwargs)


def test_a_response_factor_of_zero_uses_the_floor_only() -> None:
    assert SteadinessRule(response_factor=0).window_for(100) == 30.0


def test_a_judge_needs_a_channel_and_a_full_scale() -> None:
    with pytest.raises(FujiValidationError, match="at least one channel"):
        SteadinessJudge(SteadinessRule(), {})
    bad = SteadinessTarget(gas=0.0, full_scale=0.0, unit="vol%")
    with pytest.raises(FujiValidationError, match="full scale must be positive"):
        SteadinessJudge(SteadinessRule(), {CH3: bad})


def test_a_gas_that_holds_still_for_the_window_is_steady() -> None:
    j = judge()
    verdicts = [j.feed(float(t), {CH3: 20.94}) for t in range(30)]
    assert not any(v.steady for v in verdicts)
    assert verdicts[-1].channels[CH3].reason == "steady so far, for 29.0 of 30 s"
    verdict = j.feed(30.0, {CH3: 20.95})
    assert verdict.steady
    channel = verdict.channels[CH3]
    assert channel.covered_s == 30.0
    assert channel.spread_percent_fs == approx(100 * 0.01 / 21)
    assert channel.reason.startswith("steady: moved 0.05 %FS over 30 s")
    assert (verdict.elapsed_s, verdict.reads) == (30.0, 31)
    assert verdict.reasons == (f"CH3: {channel.reason}",)


def test_a_reading_still_moving_is_not_steady() -> None:
    j = judge()
    verdict = last(j, ((float(t), {CH3: 20.0 + t * 0.01}) for t in range(61)))  # 0.6 vol%/min
    assert not verdict.steady
    assert "moved 1.43 %FS over the last 30.0 s" in verdict.channels[CH3].reason


def test_a_steady_reading_far_from_the_named_gas_is_the_wrong_gas() -> None:
    j = judge()
    verdict = last(j, ((float(t), {CH3: 0.02}) for t in range(40)))  # zero gas, span gas named
    assert not verdict.steady
    reason = verdict.channels[CH3].reason
    assert reason == "reads 0.02 vol%, -99.7 %FS from the named gas 20.95 (at most ±10 %FS)"
    assert verdict.channels[CH3].offset_percent_fs == approx(-99.66666, abs=1e-3)


def test_an_undecoded_reading_in_the_window_is_not_steady() -> None:
    j = judge(SteadinessRule(window_s=2, response_factor=0))
    j.feed(0.0, {CH3: 20.95})
    verdict = j.feed(1.0, {CH3: None})
    assert verdict.channels[CH3].reason == "a reading in the window could not be decoded"
    assert verdict.channels[CH3].mean is None
    verdict = j.feed(2.0, {})  # a channel missing from the read counts as undecoded
    assert not verdict.steady
    verdict = last(j, ((t, {CH3: 20.95}) for t in (3.0, 4.0, 5.0, 6.0)))
    assert verdict.steady  # the bad reads have left the window


def test_every_channel_must_be_steady_at_once() -> None:
    co2 = SteadinessTarget(gas=0.0, full_scale=10.0, unit="vol%")
    co = SteadinessTarget(gas=0.0, full_scale=1.0, unit="vol%")
    j = judge(SteadinessRule(window_s=5), CH1=co2, CH2=co)
    verdict = last(j, ((float(t), {CH1: 0.0, CH2: 0.5 - 0.1 * t}) for t in range(6)))
    assert verdict.channels[CH1].steady
    assert not verdict.channels[CH2].steady
    assert not verdict.steady


def test_reads_must_come_in_time_order_and_reset_forgets_them() -> None:
    j = judge(SteadinessRule(window_s=1))
    j.feed(5.0, {CH3: 20.95})
    with pytest.raises(FujiValidationError, match="time order"):
        j.feed(4.0, {CH3: 20.95})
    j.reset()
    verdict = j.feed(1.0, {CH3: 20.95})
    assert (verdict.reads, verdict.elapsed_s, verdict.steady) == (1, 0.0, False)
    assert j.rule.window_s == 1
    assert set(j.targets) == {CH3}


def test_old_reads_leave_the_window_but_one_covers_its_start() -> None:
    j = judge(SteadinessRule(window_s=2, response_factor=0))
    verdict = last(j, ((t, {CH3: 20.95}) for t in (0.0, 0.5, 1.0, 1.5, 2.5, 3.0, 3.5, 4.5)))
    channel = verdict.channels[CH3]
    assert channel.covered_s == 2.0
    assert channel.samples == 4  # 2.5, where the window starts, to 4.5; the older reads are gone
    assert verdict.steady


@given(
    start=st.floats(min_value=0.0, max_value=21.0),
    tau=st.floats(min_value=1.0, max_value=60.0),
)
def test_an_approach_to_the_gas_is_never_steady_before_the_window_is_covered(
    start: float, tau: float
) -> None:
    j = judge(SteadinessRule(window_s=30, response_factor=0))
    for t in range(0, 30):
        value = round(20.95 + (start - 20.95) * math.exp(-t / tau), 2)
        assert not j.feed(float(t), {CH3: value}).steady


@given(noise=st.lists(st.integers(min_value=-5, max_value=5), min_size=31, max_size=31))
def test_noise_within_the_band_on_the_gas_is_steady(noise: list[int]) -> None:
    # 0.5 %FS of 21 vol% is 0.105 vol%: noise of +-0.05 never spans more.
    j = judge(SteadinessRule(window_s=30, response_factor=0))
    assert last(j, ((float(t), {CH3: 20.95 + n / 100}) for t, n in enumerate(noise))).steady


def test_a_verdict_never_rests_on_reads_either_side_of_a_pause() -> None:
    # Steady reads, then a minute with none (an operator answering a prompt), then one more.
    j = judge(SteadinessRule(window_s=30, response_factor=0))
    last(j, ((float(t), {CH3: 20.95}) for t in range(31)))
    verdict = j.feed(90.0, {CH3: 20.95})
    assert not verdict.steady
    assert verdict.channels[CH3].reason == "reads 60.0 s apart in the window (at most 5 s)"
    verdict = last(j, ((90.0 + t, {CH3: 20.95}) for t in range(1, 31)))
    assert verdict.steady  # read closely again for a whole window
