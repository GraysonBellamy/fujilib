"""Encoding a caller's value for a setting write, before and after its range is read.

Design §6.1: what needs no I/O is checked first, the rest against the range.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from fujilib.devices.encode import PreparedValue, encode_prepared, prepare_value
from fujilib.errors import FujiValidationError
from fujilib.registry.enums import HoldMode, RangeIndex, RangeMethod
from fujilib.registry.registers import REGISTRY, RegisterSpec
from fujilib.registry.units import Unit

VOL = Unit.VOL_PERCENT
#: Ch3 range 1 on the bench: 0-21.00 vol%.
CH3_RANGE1 = (VOL, 21.0, 2)


def spec(name: str) -> RegisterSpec:
    return REGISTRY.resolve(name)


# --- Unscaled values: encoded before any I/O -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "raw"),
    [
        ("output_hold.enabled", True, 1),
        ("output_hold.enabled", False, 0),
        ("hold.mode", HoldMode.SETTING, 1),
        ("hold.mode", "setting", 1),
        ("hold.mode", " Last-Value ", 0),
        ("range.ch3.selected", RangeIndex.RANGE_2, 1),
        ("range.ch3.selected", "range_2", 1),
        ("range.ch1.method", "auto", 2),
        ("response_time.o2", 16, 16),
        ("response_time.o2", 16.0, 16),
        ("hold.ch5.value", 0, 0),
        ("hold.ch5.value", 100, 100),
    ],
)
def test_unscaled_values_encode_at_once(name: str, value: object, raw: int) -> None:
    prepared = prepare_value(spec(name), value)
    assert prepared.raw == raw
    assert encode_prepared(prepared) == raw


def test_a_unit_may_be_given_for_an_unscaled_value_if_it_is_the_registers() -> None:
    assert prepare_value(spec("response_time.o2"), 20, unit="s").raw == 20
    assert prepare_value(spec("hold.ch1.value"), 20, unit="%FS").raw == 20
    with pytest.raises(FujiValidationError, match="takes no unit 'ms'"):
        prepare_value(spec("response_time.o2"), 20, unit="ms")
    with pytest.raises(FujiValidationError, match="its unit is none"):
        prepare_value(spec("output_hold.enabled"), True, unit="s")


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("output_hold.enabled", 1, "True or False"),
        ("output_hold.enabled", "on", "True or False"),
        # A bare number for an enum is refused: RangeIndex 1 is range 2.
        ("range.ch3.selected", 1, "one of range_1, range_2"),
        ("range.ch3.selected", "range_3", "RangeIndex"),
        ("hold.mode", True, "one of last_value, setting"),
        ("range.ch1.method", RangeMethod.REMOTE, "remote cannot be written; only manual, auto"),
        ("range.ch1.method", "remote", "only manual, auto"),
        ("response_time.o2", 0, "outside 1-60 s"),
        ("response_time.o2", 61, "outside 1-60 s"),
        ("response_time.o2", 15.5, "whole number"),
        ("response_time.o2", True, "whole number"),
        ("response_time.o2", "15", "whole number"),
        ("response_time.o2", float("nan"), "whole number"),
        ("hold.ch1.value", 101, "outside 0-100 %FS"),
    ],
)
def test_values_that_do_not_fit_are_refused(name: str, value: object, match: str) -> None:
    with pytest.raises(FujiValidationError, match=match) as info:
        prepare_value(spec(name), value)
    assert info.value.context.extra["setting"] == name


@pytest.mark.parametrize("name", ["key_lock", "alarm1.mode", "reading.ch1.value", "clock.year"])
def test_read_only_registers_are_refused(name: str) -> None:
    with pytest.raises(FujiValidationError, match="read-only"):
        prepare_value(spec(name), 1)


def test_write_values_on_an_unscaled_non_enum_register() -> None:
    # No such register ships; the rule still names the values.
    only_five = dataclasses.replace(spec("response_time.o2"), write_values=frozenset({5}))
    assert prepare_value(only_five, 5).raw == 5
    with pytest.raises(FujiValidationError, match="6 cannot be written; only 5"):
        prepare_value(only_five, 6)


def test_limits_default_to_a_word() -> None:
    open_ended = dataclasses.replace(spec("response_time.o2"), minimum=None, maximum=None)
    assert prepare_value(open_ended, 0xFFFF).raw == 0xFFFF
    with pytest.raises(FujiValidationError, match="outside 0-65535"):
        prepare_value(open_ended, 0x10000)


# --- Range-scaled values: finished against the range read just before the write -------------


@pytest.mark.parametrize(
    ("value", "raw"),
    [(20.95, 2095), ("20.95", 2095), (Decimal("20.9"), 2090), (21, 2100), (0.21, 21)],
)
def test_a_calibration_gas_is_encoded_with_its_ranges_decimals(value: object, raw: int) -> None:
    prepared = prepare_value(spec("calibration_gas.ch3.range1.span"), value, unit="vol%")
    assert prepared.raw is None
    assert prepared.unit is VOL
    assert encode_prepared(prepared, CH3_RANGE1) == raw


def test_a_calibration_gas_needs_its_unit() -> None:
    gas = spec("calibration_gas.ch3.range1.span")
    with pytest.raises(FujiValidationError, match="give the unit"):
        prepare_value(gas, 20.95)
    with pytest.raises(FujiValidationError, match="unknown unit 'furlongs'"):
        prepare_value(gas, 20.95, unit="furlongs")


@pytest.mark.parametrize("value", [True, None, "twenty", float("inf"), float("nan"), -1, [1]])
def test_a_calibration_gas_must_be_a_finite_non_negative_number(value: object) -> None:
    with pytest.raises(FujiValidationError, match="number"):
        prepare_value(spec("calibration_gas.ch3.range1.span"), value, unit="vol%")


def test_a_calibration_gas_in_another_unit_than_its_range_is_refused() -> None:
    # The guard against a vol%/ppm slip of 10^4 (design D4).
    prepared = prepare_value(spec("calibration_gas.ch3.range1.span"), 2095, unit="ppm")
    with pytest.raises(FujiValidationError, match="is in vol%, not ppm"):
        encode_prepared(prepared, CH3_RANGE1)
    with pytest.raises(FujiValidationError, match=r"is in \?, not vol%"):
        encode_prepared(
            prepare_value(spec("calibration_gas.ch3.range1.span"), 1, unit="vol%"),
            (Unit.UNKNOWN, 21.0, 2),
        )


def test_a_calibration_gas_needs_its_range() -> None:
    prepared = prepare_value(spec("calibration_gas.ch3.range1.span"), 20.95, unit="vol%")
    with pytest.raises(FujiValidationError, match="decimal point and unit are not known"):
        encode_prepared(prepared)


def test_a_calibration_gas_with_more_decimals_than_its_range_is_refused() -> None:
    prepared = prepare_value(spec("calibration_gas.ch3.range1.span"), 20.955, unit="vol%")
    with pytest.raises(FujiValidationError, match="more than 2 decimal"):
        encode_prepared(prepared, CH3_RANGE1)


@pytest.mark.parametrize(
    ("name", "value", "ok"),
    [
        ("calibration_gas.ch3.range1.span", 0.21, True),  # 1 %FS
        ("calibration_gas.ch3.range1.span", 0.20, False),
        ("calibration_gas.ch3.range1.span", 22.05, True),  # 105 %FS
        ("calibration_gas.ch3.range1.span", 22.06, False),
        ("calibration_gas.ch3.range1.zero", 0, True),
        ("calibration_gas.ch3.range1.zero", 21.0, True),  # 100 %FS
        ("calibration_gas.ch3.range1.zero", 21.01, False),
    ],
)
def test_calibration_gases_stay_within_percent_of_full_scale(
    name: str, value: float, ok: bool
) -> None:
    prepared = prepare_value(spec(name), value, unit="vol%")
    if ok:
        encode_prepared(prepared, CH3_RANGE1)
    else:
        with pytest.raises(FujiValidationError, match=r"% of the range's full scale"):
            encode_prepared(prepared, CH3_RANGE1)


def test_a_range_without_a_full_scale_refuses_a_percent_limited_value() -> None:
    prepared = prepare_value(spec("calibration_gas.ch3.range1.zero"), 0, unit="vol%")
    for full_scale in (0.0, float("nan")):
        with pytest.raises(FujiValidationError, match="full scale"):
            encode_prepared(prepared, (VOL, full_scale, 2))


def test_raw_limits_apply_to_a_scaled_value_too() -> None:
    # 99.99 is 9999 at two decimals; 100.00 does not fit the register.
    wide = dataclasses.replace(spec("calibration_gas.ch3.range1.span"), write_percent_fs=None)
    prepared = prepare_value(wide, 100, unit="vol%")
    with pytest.raises(FujiValidationError, match="outside 0-9999"):
        encode_prepared(prepared, CH3_RANGE1)


def test_a_scaled_value_without_percent_limits_is_limited_by_its_register() -> None:
    wide = dataclasses.replace(spec("calibration_gas.ch3.range1.span"), write_percent_fs=None)
    assert encode_prepared(prepare_value(wide, 50, unit="vol%"), CH3_RANGE1) == 5000


def test_a_prepared_value_is_frozen() -> None:
    prepared = prepare_value(spec("response_time.o2"), 16)
    assert isinstance(prepared, PreparedValue)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.raw = 1  # type: ignore[misc]
