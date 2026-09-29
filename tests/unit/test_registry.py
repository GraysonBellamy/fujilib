"""The register map: addresses against the manual, invariants, and validation (design §5.1)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from fujilib.devices.capability import Capability, SafetyTier
from fujilib.errors import FujiConfigurationError, FujiValidationError
from fujilib.protocol.modbus.codec import DataType
from fujilib.registry.channels import ChannelId
from fujilib.registry.enums import AlarmMode
from fujilib.registry.regions import (
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    ZP_REGIONS,
    Evidence,
    RegisterTable,
)
from fujilib.registry.registers import (
    CALIBRATION_LOG,
    ERROR_LOG,
    REGISTRY,
    Access,
    LogField,
    RegisterSpec,
    Scaling,
    ScalingKind,
    validate_map,
)
from fujilib.registry.write_policy import KEY_SIMULATION_ADDRESS, envelope_allows

# --- Addresses the manual states outright ----------------------------------------


@pytest.mark.parametrize(
    ("name", "table", "address", "number"),
    [
        # Worked examples (TN5A1190a p.20-26).
        ("calibration_gas.ch1.range1.span", RegisterTable.HOLDING, 0x01, 40002),
        ("calibration_gas.ch2.range1.zero", RegisterTable.HOLDING, 0x04, 40005),
        ("alarm1.range1.high", RegisterTable.HOLDING, 0x23, 40036),
        ("reading.ch3.value", RegisterTable.INPUT, 0x06, 30007),
        ("reading.ch3.decimals", RegisterTable.INPUT, 0x07, 30008),
        ("reading.ch5.value", RegisterTable.INPUT, 0x0C, 30013),
        ("range.ch1.range1.unit", RegisterTable.INPUT, 0x42A, 31067),
        ("range.ch1.range1.full_scale", RegisterTable.INPUT, 0x434, 31077),
        ("range.ch1.range1.decimals", RegisterTable.INPUT, 0x43E, 31087),
        ("range.ch5.range2.decimals", RegisterTable.INPUT, 0x447, 31096),
        # Last or elided rows the strides must reach.
        ("calibration_gas.ch5.range2.span", RegisterTable.HOLDING, 0x13, 40020),
        ("response_time.o2", RegisterTable.HOLDING, 0x53, 40084),
        ("alarm6.target_channel", RegisterTable.HOLDING, 0x7D, 40126),
        ("o2_correction.limit", RegisterTable.HOLDING, 0x9D, 40158),
        ("reading.ch12.unit", RegisterTable.INPUT, 0x23, 30036),
        ("status.calibration_error", RegisterTable.INPUT, 0x3C, 30061),
        ("error.e10.active", RegisterTable.INPUT, 0x86, 30135),
        ("error.ch4.e4.active", RegisterTable.INPUT, 0x99, 30154),
        ("error.ch5.e4.active", RegisterTable.INPUT, 0x9F, 30160),
        ("error.ch5.e9.active", RegisterTable.INPUT, 0xA4, 30165),
        ("status.ch4.auto_zero_running", RegisterTable.INPUT, 0xAE, 30175),
        ("status.ch5.hold", RegisterTable.INPUT, 0xB3, 30180),
        ("alarm6.state", RegisterTable.INPUT, 0xBE, 30191),
        ("identity.type_code", RegisterTable.INPUT, 0x448, 31097),
        ("identity.serial_number", RegisterTable.INPUT, 0x462, 31123),
        ("identity.type_code_ext", RegisterTable.INPUT, 0x47A, 31147),
        # Observed on the bench unit.
        ("clock.year", RegisterTable.INPUT, 0x3E8, 31001),
        ("adc.resistance4_2", RegisterTable.INPUT, 0x417, 31048),
    ],
)
def test_addresses(name: str, table: RegisterTable, address: int, number: int) -> None:
    spec = REGISTRY.resolve(name)
    assert spec.table is table
    assert spec.address == address
    assert spec.register_number == number


def test_log_layouts() -> None:
    assert ERROR_LOG.record_address(1, 13) == 0x7E  # the manual's oldest entry
    assert ERROR_LOG.last_address == 0x82
    assert CALIBRATION_LOG.record_address(1, 39) == 0x115F
    assert CALIBRATION_LOG.record_address(5, 0) == 0x15A0
    assert CALIBRATION_LOG.last_address == 0x1707
    assert CALIBRATION_LOG.words_per_channel == 360
    with pytest.raises(FujiValidationError):
        CALIBRATION_LOG.record_address(6, 0)
    with pytest.raises(FujiValidationError):
        ERROR_LOG.record_address(1, 14)
    assert REGISTRY.log("error_log") is ERROR_LOG
    with pytest.raises(FujiValidationError):
        REGISTRY.log("event_log")


# --- Invariants over the whole map ---------------------------------------------------


def test_size_and_groups() -> None:
    assert len(REGISTRY) == 345
    assert len(REGISTRY.groups()) == len(set(REGISTRY.groups()))
    assert len(REGISTRY.select("calibration_gas")) == 20
    assert len(REGISTRY.select("reading")) == 36
    assert REGISTRY.select("key_lock") == (REGISTRY.resolve("key_lock"),)


def test_map_is_in_table_then_address_order() -> None:
    holding = REGISTRY.in_table(RegisterTable.HOLDING)
    inputs = REGISTRY.in_table(RegisterTable.INPUT)
    assert REGISTRY.specs == holding + inputs
    for specs in (holding, inputs):
        assert [s.address for s in specs] == sorted(s.address for s in specs)


def test_only_documented_holding_registers_are_writable() -> None:
    for spec in REGISTRY:
        if spec.writable:
            assert spec.table is RegisterTable.HOLDING
            assert spec.evidence is Evidence.DOCUMENTED
            assert spec.safety >= SafetyTier.PERSISTENT
        else:
            assert spec.safety is SafetyTier.READ_ONLY
            assert not spec.write_functions


def test_every_write_function_is_inside_the_envelope() -> None:
    for spec in REGISTRY:
        for fc in spec.write_functions:
            assert envelope_allows(fc, spec.address, spec.count), spec.name
        if FC_WRITE_SINGLE in spec.write_functions:
            assert spec.last_address <= 0x9D
        assert not spec.address <= KEY_SIMULATION_ADDRESS <= spec.last_address


def test_function_code_choice() -> None:
    assert REGISTRY.resolve("hold.ch5.value").write_functions == {
        FC_WRITE_SINGLE,
        FC_WRITE_MULTIPLE,
    }
    assert REGISTRY.resolve("reference_gas.switching_time").write_functions == frozenset()
    for spec in REGISTRY.select("interference"):
        assert spec.access is Access.READ
        assert spec.evidence is Evidence.INFERRED
        assert spec.dtype is DataType.UINT32_LH


#: The reviewed write subset (design §5.4), spelled out so any change to it is deliberate.
WRITABLE = frozenset(
    [
        *(
            f"calibration_gas.ch{c}.range{r}.{kind}"
            for c in range(1, 6)
            for r in (1, 2)
            for kind in ("zero", "span")
        ),
        *(f"calibration.ch{c}.{mode}" for c in range(1, 6) for mode in ("zero_mode", "range_mode")),
        *(f"response_time.ndir{k}" for k in range(1, 5)),
        "response_time.o2",
        "output_hold.enabled",
        "hold.mode",
        *(f"hold.ch{c}.value" for c in range(1, 6)),
        *(f"range.ch{c}.{part}" for c in range(1, 6) for part in ("selected", "method")),
    ]
)


def test_the_writable_subset_is_exactly_the_reviewed_one() -> None:
    assert len(WRITABLE) == 52
    assert {s.name for s in REGISTRY if s.writable} == WRITABLE
    for spec in REGISTRY:
        if spec.writable:
            assert spec.requires is Capability.NONE, spec.name
            assert spec.count == 1


@pytest.mark.parametrize(
    "prefix",
    [
        "alarm1",
        "alarm6",
        "alarm.hysteresis",
        "auto_calibration",
        "auto_zero",
        "blowback",
        "key_lock",
        "moving_average1",
        "o2_correction",
        "peak_alarm",
        "measurement_point",
        "reference_gas",
    ],
)
def test_settings_outside_the_subset_are_read_only(prefix: str) -> None:
    specs = REGISTRY.select(prefix)
    assert specs
    assert not any(s.writable for s in specs)


def test_write_limits() -> None:
    assert (
        REGISTRY.resolve("response_time.o2").minimum,
        REGISTRY.resolve("response_time.o2").maximum,
    ) == (1, 60)
    assert REGISTRY.resolve("range.ch3.method").write_values == frozenset({0, 2})
    assert REGISTRY.resolve("calibration_gas.ch3.range1.span").write_percent_fs == (1, 105)
    assert REGISTRY.resolve("calibration_gas.ch3.range1.zero").write_percent_fs == (0, 100)


@pytest.mark.parametrize(
    ("prefix", "tier"),
    [
        ("calibration_gas", SafetyTier.DANGEROUS),
        ("calibration", SafetyTier.DANGEROUS),
        ("response_time", SafetyTier.PERSISTENT),
        ("output_hold", SafetyTier.PERSISTENT),
        ("hold", SafetyTier.PERSISTENT),
        ("range.ch1.method", SafetyTier.PERSISTENT),
    ],
)
def test_safety_tiers_follow_effect(prefix: str, tier: SafetyTier) -> None:
    writable = [s for s in REGISTRY.select(prefix) if s.writable]
    assert writable
    assert all(s.safety is tier for s in writable)


def test_contested_registers_are_read_only() -> None:
    contested = [s.name for s in REGISTRY if s.evidence is Evidence.CONTESTED]
    assert "auto_calibration.start_hour" in contested
    assert "blowback.start_minute" in contested
    assert "alarm6.target_channel" in contested
    assert all(not REGISTRY.resolve(n).writable for n in contested)


def test_every_register_has_a_reference_and_doc() -> None:
    for spec in REGISTRY:
        assert spec.manual_ref
        assert spec.doc


def test_scaled_registers_carry_their_source() -> None:
    for spec in REGISTRY:
        if spec.scaling.kind is ScalingKind.BY_RANGE:
            assert spec.channel is not None
            assert spec.range in {1, 2}
        if spec.scaling.kind is ScalingKind.INLINE:
            assert spec.name.startswith("reading.")
    assert str(Scaling(ScalingKind.FIXED, 1)) == "fixed(1)"
    assert str(REGISTRY.resolve("alarm2.range1.low").scaling) == "by_alarm_target"


def test_measured_channel_registers_stop_at_channel_5() -> None:
    for spec in REGISTRY:
        if spec.channel is not None and not spec.name.startswith("reading."):
            assert spec.channel.is_measured, spec.name


# --- Lookup --------------------------------------------------------------------------


def test_resolve_suggests_close_names() -> None:
    with pytest.raises(FujiValidationError, match=r"did you mean .*alarm1\.mode"):
        REGISTRY.resolve("alarm1.mod")
    assert REGISTRY.has("key_lock")
    assert not REGISTRY.has("keylock")
    assert "key_lock" in REGISTRY


def test_at_finds_multi_word_values() -> None:
    type_code = REGISTRY.resolve("identity.type_code")
    assert REGISTRY.at(RegisterTable.INPUT, 0x448 + 25) is type_code
    assert REGISTRY.at(RegisterTable.HOLDING, 0xA5) is REGISTRY.resolve("interference.coefficient1")
    assert REGISTRY.at(RegisterTable.HOLDING, 0x48) is None  # "not used"


# --- validate_map rejects broken tables ------------------------------------------------


def _good() -> RegisterSpec:
    return REGISTRY.resolve("hold.mode")


def _bad(**changes: Any) -> RegisterSpec:
    return dataclasses.replace(_good(), **changes)


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        (_bad(address=0x0120), "not inside"),
        (_bad(write_functions=frozenset({0x05})), "not inside"),
        (_bad(evidence=Evidence.OBSERVED), "only documented"),
        (_bad(safety=SafetyTier.STATEFUL), "PERSISTENT or DANGEROUS"),
        (_bad(address=0x0A4, write_functions=frozenset({FC_WRITE_MULTIPLE})), "envelope"),
        (_bad(access=Access.READ), "read-only but"),
        (_bad(manual_ref=""), "reference"),
        (_bad(minimum=5, maximum=1, enum=None, dtype=DataType.UINT16), "minimum exceeds"),
        (_bad(enum=None), "ENUM type"),
        (_bad(maximum=9), "limits do not match HoldMode"),
        (_bad(dtype=DataType.BOOL, enum=None, minimum=0, maximum=4), "0..1"),
        (_bad(scaling=Scaling(ScalingKind.FIXED)), "fixed scaling"),
        (_bad(scaling=Scaling(ScalingKind.BY_RANGE)), "range scaling"),
        (_bad(scaling=Scaling(ScalingKind.BY_ALARM_TARGET), range=None), "alarm-target"),
        (_bad(scaling=Scaling(ScalingKind.INLINE)), "inline scaling"),
        (_bad(count=2), "count 2"),
        (_bad(name="hold.mode.copy"), "only the reviewed settings"),
        (_bad(address=0x8A), "only the reviewed settings"),
        (_bad(write_functions=frozenset({FC_WRITE_MULTIPLE})), "one word that FC06"),
        (_bad(write_values=frozenset({7})), "write values"),
        (_bad(write_values=frozenset()), "write values"),
        (_bad(write_percent_fs=(0, 100)), "need range scaling"),
        (
            _bad(
                scaling=Scaling(ScalingKind.BY_RANGE),
                channel=ChannelId.CH1,
                range=1,
                write_percent_fs=(5, 1),
            ),
            "0 <= low <= high",
        ),
        (
            _bad(
                access=Access.READ,
                write_functions=frozenset(),
                safety=SafetyTier.READ_ONLY,
                write_values=frozenset({0}),
            ),
            "read-only but has write limits",
        ),
        (_bad(read_functions=frozenset({0x04})), "read functions"),
        (
            _bad(table=RegisterTable.INPUT, read_functions=frozenset({0x04})),
            "not a holding register",
        ),
        (
            _bad(address=0x1000, table=RegisterTable.INPUT, read_functions=frozenset({0x04})),
            "requires",
        ),
    ],
)
def test_validate_map_rejects(spec: RegisterSpec, match: str) -> None:
    with pytest.raises(FujiConfigurationError, match=match):
        validate_map([spec], [], ZP_REGIONS)


def test_validate_map_rejects_duplicates_and_overlaps() -> None:
    good = REGISTRY.resolve("alarm1.mode")  # read-only, so a renamed copy is not refused for that
    with pytest.raises(FujiConfigurationError, match="duplicate"):
        validate_map([good, good], [], ZP_REGIONS)
    with pytest.raises(FujiConfigurationError, match="overlap"):
        validate_map([good, dataclasses.replace(good, name="alias")], [], ZP_REGIONS)
    first_error_word = REGISTRY.resolve("status.calibration_error")
    with pytest.raises(FujiConfigurationError, match="overlap"):
        validate_map([dataclasses.replace(first_error_word, address=0x3D)], [ERROR_LOG], ZP_REGIONS)
    with pytest.raises(FujiConfigurationError, match="duplicate name"):
        validate_map([dataclasses.replace(good, name="error_log")], [ERROR_LOG], ZP_REGIONS)


def test_validate_map_rejects_bad_logs() -> None:
    with pytest.raises(FujiConfigurationError, match="not inside one region"):
        validate_map([], [dataclasses.replace(ERROR_LOG, base=0x00A0)], ZP_REGIONS)
    too_wide = dataclasses.replace(
        ERROR_LOG, fields=(LogField("x", 4, DataType.UINT32_LH, "", count=2),)
    )
    with pytest.raises(FujiConfigurationError, match="outside the record"):
        validate_map([], [too_wide], ZP_REGIONS)


def test_enum_limits_come_from_the_enum() -> None:
    spec = REGISTRY.resolve("alarm1.mode")
    assert spec.enum is AlarmMode
    assert (spec.minimum, spec.maximum) == (0, 4)
    assert spec.requires is Capability.ALARMS
    assert REGISTRY.resolve("range.ch2.selected").channel is ChannelId.CH2
