"""Read planner: exact hot-path transactions and invariants (design §4.3)."""

from __future__ import annotations

import dataclasses
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fujilib.errors import FujiConfigurationError, FujiValidationError
from fujilib.protocol.modbus import read_plan as rp
from fujilib.protocol.modbus.read_plan import (
    DEFAULT_READ_POLICY,
    BlockRead,
    ReadPolicy,
    calibration_log_plan,
    plan_log_reads,
    plan_reads,
)
from fujilib.registry.regions import ZP_REGIONS
from fujilib.registry.registers import CALIBRATION_LOG, ERROR_LOG, REGISTRY, RegisterSpec

FC03, FC04 = 0x03, 0x04


def keys(plan: tuple[BlockRead, ...]) -> list[tuple[int, int, int]]:
    return [b.key for b in plan]


# --- The hot paths ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (rp.POLL_PLAN, [(FC04, 0x0000, 61), (FC04, 0x0083, 60)]),
        (rp.IDENTIFY_PLAN, [(FC04, 0x0425, 35), (FC04, 0x0448, 34), (FC04, 0x0000, 42)]),
        (rp.RANGES_PLAN, [(FC04, 0x0425, 35)]),
        (rp.METADATA_PLAN, [(FC03, 0x0000, 64), (FC03, 0x0040, 64), (FC03, 0x0080, 36)]),
        (rp.SETTINGS_PLAN, [(FC03, 0x0000, 64), (FC03, 0x0040, 64), (FC03, 0x0080, 44)]),
        (rp.CLOCK_PLAN, [(FC04, 0x03E8, 7)]),
        (rp.ADC_PLAN, [(FC04, 0x03EF, 42)]),
        (rp.SERVICE_PLAN, [(FC04, 0x03E8, 49)]),
        (rp.TYPE_CODE_EXT_PLAN, [(FC04, 0x047A, 3)]),
        ((rp.CALIBRATION_LOG_PROBE,), [(FC04, 0x1000, 9)]),
        (rp.ERROR_LOG_PLAN, [(FC04, 0x003D, 60), (FC04, 0x0079, 10)]),
    ],
)
def test_named_plans(plan: tuple[BlockRead, ...], expected: list[tuple[int, int, int]]) -> None:
    assert keys(plan) == expected


@pytest.mark.parametrize("channel", range(1, 6))
def test_calibration_log_plan(channel: int) -> None:
    plan = calibration_log_plan(channel)
    base = 0x1000 + 360 * (channel - 1)
    assert keys(plan) == [
        *((FC04, base + 63 * k, 63) for k in range(5)),
        (FC04, base + 315, 45),
    ]
    assert plan[-1].last_address == base + 359
    for block in plan:
        assert (block.address - base) % CALIBRATION_LOG.record_words == 0
        assert block.count % CALIBRATION_LOG.record_words == 0


def test_poll_block_1_is_every_concentration_and_the_status_summary() -> None:
    first = rp.POLL_PLAN[0]
    names = {s.name for s in first.specs}
    assert {f"reading.ch{n}.value" for n in range(1, 13)} <= names
    assert "status.calibration_error" in names
    assert "status.ch1.hold" not in names  # hold flags are in block 2


# --- Invariants ----------------------------------------------------------------------

_SPECS = list(REGISTRY)


@settings(max_examples=200)
@given(st.lists(st.sampled_from(_SPECS), min_size=1, max_size=60, unique_by=lambda s: s.name))
def test_plan_invariants(specs: list[RegisterSpec]) -> None:
    plan = plan_reads(specs)
    for block in plan:
        assert 1 <= block.count <= DEFAULT_READ_POLICY.max_words
        assert ZP_REGIONS.region_for(block.function, block.address, block.count) is not None
        for spec in block.specs:
            assert spec.table.read_function == block.function
            assert block.address <= spec.address <= spec.last_address <= block.last_address
    covered = [s.name for b in plan for s in b.specs]
    assert sorted(covered) == sorted(s.name for s in specs)  # each exactly once
    assert block_order_is_stable(plan)


def block_order_is_stable(plan: tuple[BlockRead, ...]) -> bool:
    return [b.key for b in plan] == sorted((b.key for b in plan), key=lambda k: (k[0], k[1]))


@given(st.lists(st.sampled_from(_SPECS), min_size=1, max_size=30, unique_by=lambda s: s.name))
def test_strict_policy_never_bridges(specs: list[RegisterSpec]) -> None:
    for block in plan_reads(specs, policy=DEFAULT_READ_POLICY.strict()):
        ordered = sorted(block.specs, key=lambda s: s.address)
        for before, after in pairwise(ordered):
            assert after.address <= before.last_address + 1


def test_blocks_close_at_region_boundaries() -> None:
    # 0418h (A/D) and 0425h (ranges) are 13 words apart but in different regions.
    specs = [REGISTRY.resolve("adc.resistance4_2"), REGISTRY.resolve("range.ch1.count")]
    wide = ReadPolicy(max_gap=64)
    assert keys(plan_reads(specs, policy=wide)) == [(FC04, 0x0417, 2), (FC04, 0x0425, 1)]


def test_multi_word_values_are_never_split() -> None:
    # A two-word limit fits the long word whole rather than packing its first half.
    specs = [REGISTRY.resolve("clock.second"), REGISTRY.resolve("adc.input1")]
    plan = plan_reads(specs, policy=ReadPolicy(max_words=2))
    assert keys(plan) == [(FC04, 0x03EE, 1), (FC04, 0x03EF, 2)]


# --- Errors and helpers ----------------------------------------------------------------


def test_register_wider_than_a_request_is_refused() -> None:
    with pytest.raises(FujiConfigurationError):
        plan_reads([REGISTRY.resolve("identity.type_code")], policy=ReadPolicy(max_words=20))


def test_register_outside_every_region_is_refused() -> None:
    stray = dataclasses.replace(REGISTRY.resolve("peak_alarm.active"), address=0x0200)
    with pytest.raises(FujiConfigurationError):
        plan_reads([stray])


@pytest.mark.parametrize("policy", [{"max_words": 0}, {"max_gap": -1}])
def test_policy_validation(policy: dict[str, int]) -> None:
    with pytest.raises(FujiConfigurationError):
        ReadPolicy(**policy)


def test_log_records_must_fit() -> None:
    with pytest.raises(FujiConfigurationError):
        plan_log_reads(CALIBRATION_LOG, 1, policy=ReadPolicy(max_words=8))
    with pytest.raises(FujiValidationError):
        plan_log_reads(ERROR_LOG, 2)


def test_block_slicing() -> None:
    block = rp.IDENTIFY_PLAN[1]  # type code and serial
    words = tuple(range(block.count))
    serial = REGISTRY.resolve("identity.serial_number")
    assert block.words_for(serial, words) == tuple(range(26, 34))
    bank = block.to_bank(words)
    assert bank[0x0448] == 0
    assert bank[0x0469] == 33
    with pytest.raises(FujiValidationError):
        block.words_for(REGISTRY.resolve("reading.ch1.value"), words)
    with pytest.raises(FujiValidationError):
        block.to_bank(words[:-1])
    with pytest.raises(FujiValidationError):
        block.words_for(serial, words[:-1])
