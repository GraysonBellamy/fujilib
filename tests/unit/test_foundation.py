"""Channels, units, register enums, capability flags and serial settings."""

from __future__ import annotations

import pytest
from anyserial import ByteSize, Parity, StopBits

from fujilib.devices.capability import (
    PROBED_CAPABILITIES,
    Availability,
    Capability,
    SafetyTier,
)
from fujilib.errors import FujiValidationError
from fujilib.protocol.base import ProtocolKind
from fujilib.registry.channels import (
    CHANNELS,
    MEASURED_CHANNELS,
    ChannelId,
    Gas,
    coerce_channel,
)
from fujilib.registry.enums import (
    AlarmMode,
    AlarmState,
    CalibrationKind,
    ErrorCode,
    ErrorScope,
    PeriodUnit,
    ScheduleCycleUnit,
)
from fujilib.registry.units import Unit, coerce_unit, unit_code, unit_from_code
from fujilib.transport.base import FUJI_BAUDRATE, SerialSettings
from fujilib.units import to_pint

# --- Channels --------------------------------------------------------------


def test_twelve_channels_in_order() -> None:
    assert len(CHANNELS) == 12
    assert [c.number for c in CHANNELS] == list(range(1, 13))
    assert CHANNELS[:5] == MEASURED_CHANNELS
    assert all(c.is_measured for c in MEASURED_CHANNELS)
    assert not any(c.is_measured for c in CHANNELS[5:])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CH3", ChannelId.CH3),
        ("ch3", ChannelId.CH3),
        (" 12 ", ChannelId.CH12),
        (ChannelId.CH1, ChannelId.CH1),
    ],
)
def test_coerce_channel(text: ChannelId | str, expected: ChannelId) -> None:
    assert coerce_channel(text) is expected


@pytest.mark.parametrize("text", ["CH0", "CH13", "13", "0", "O2", ""])
def test_coerce_channel_rejects(text: str) -> None:
    with pytest.raises(FujiValidationError):
        coerce_channel(text)


def test_gas_values_match_capa_vocabulary() -> None:
    # capa's cone profile names its analyzers "o2", "co", "co2".
    assert {Gas.O2.value, Gas.CO.value, Gas.CO2.value} == {"o2", "co", "co2"}


# --- Units -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "unit"),
    [(0, Unit.VOL_PERCENT), (1, Unit.PPM), (2, Unit.MG_M3), (3, Unit.G_M3)],
)
def test_unit_codes_round_trip(code: int, unit: Unit) -> None:
    assert unit_from_code(code) is unit
    assert unit_code(unit) == code


@pytest.mark.parametrize("code", [-1, 4, 0xFFFF])
def test_unknown_unit_code_is_unknown(code: int) -> None:
    assert unit_from_code(code) is Unit.UNKNOWN


def test_unknown_unit_has_no_code() -> None:
    with pytest.raises(FujiValidationError):
        unit_code(Unit.UNKNOWN)


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("vol%", Unit.VOL_PERCENT),
        (" PPM ", Unit.PPM),
        ("mg/m³", Unit.MG_M3),
        ("bogus", Unit.UNKNOWN),
    ],
)
def test_coerce_unit(text: str, unit: Unit) -> None:
    assert coerce_unit(text) is unit


def test_to_pint_covers_every_unit() -> None:
    for unit in Unit:
        result = to_pint(unit)
        assert result is None or isinstance(result, str)
    assert to_pint(Unit.UNKNOWN) is None


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        # Strings capa's unit registry parses (design §7.8 K).
        (Unit.VOL_PERCENT, "percent"),
        ("vol%", "percent"),
        (Unit.PPM, "ppm"),
        (Unit.MG_M3, "mg/m**3"),
        ("g/m3", "g/m**3"),
        ("furlongs", None),
        (None, None),
    ],
)
def test_to_pint(unit: Unit | str | None, expected: str | None) -> None:
    assert to_pint(unit) == expected


# --- Enums -------------------------------------------------------------------


def test_alarm_mode_and_state_differ_at_two() -> None:
    assert AlarmMode(2) is AlarmMode.HIGH_OR_LOW
    assert AlarmState(2) is AlarmState.LOW


def test_the_two_cycle_units_differ() -> None:
    assert ScheduleCycleUnit(1) is ScheduleCycleUnit.DAYS
    assert PeriodUnit(1) is PeriodUnit.MINUTES


@pytest.mark.parametrize("code", list(ErrorCode))
def test_error_scope(code: ErrorCode) -> None:
    expected = ErrorScope.CHANNEL if 4 <= code <= 9 else ErrorScope.ANALYZER
    assert code.scope is expected


@pytest.mark.parametrize(
    ("kind", "rng", "span"),
    [
        (CalibrationKind.ZERO_RANGE1, 1, False),
        (CalibrationKind.SPAN_RANGE1, 1, True),
        (CalibrationKind.ZERO_RANGE2, 2, False),
        (CalibrationKind.SPAN_RANGE2, 2, True),
    ],
)
def test_calibration_kind(kind: CalibrationKind, rng: int, span: bool) -> None:
    assert kind.range == rng
    assert kind.is_span is span


# --- Capability, protocol, serial ----------------------------------------------


def test_safety_tiers_are_ordered() -> None:
    assert list(SafetyTier) == sorted(SafetyTier)
    assert int(SafetyTier.READ_ONLY) == 0
    assert SafetyTier.DANGEROUS > SafetyTier.PERSISTENT > SafetyTier.STATEFUL


def test_capability_none_and_probes() -> None:
    assert not Capability.NONE
    assert set(PROBED_CAPABILITIES) == {
        Capability.CLOCK,
        Capability.ADC_VALUES,
        Capability.TYPE_CODE_EXT,
        Capability.CALIBRATION_LOG,
    }
    assert {a.value for a in Availability} == {
        "unknown",
        "supported",
        "unsupported",
        "invalid_data",
    }


def test_protocol_kind() -> None:
    assert [p.value for p in ProtocolKind] == ["modbus_rtu"]


def test_serial_settings_default_to_the_fixed_framing() -> None:
    settings = SerialSettings(port="COM8")
    assert settings.baudrate == FUJI_BAUDRATE == 38_400
    assert settings.bytesize is ByteSize.EIGHT
    assert settings.parity is Parity.NONE
    assert settings.stopbits is StopBits.ONE
    assert not settings.rtscts
    assert not settings.xonxoff
