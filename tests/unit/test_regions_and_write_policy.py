"""Read regions and the frozen write envelope (design §2.3, §5.4)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from fujilib.devices.capability import SafetyTier
from fujilib.errors import FujiConfigurationError, FujiValidationError
from fujilib.registry.regions import (
    DOCUMENTED_REGIONS,
    FC_READ_HOLDING,
    FC_READ_INPUT,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    ZP_REGIONS,
    Evidence,
    Region,
    RegionMap,
    RegisterTable,
)
from fujilib.registry.write_policy import (
    KEY_SIMULATION_ADDRESS,
    OPERATIONS,
    WRITE_ENVELOPE,
    check_envelope,
    envelope_allows,
)

# --- Regions -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fc", "address", "count", "name"),
    [
        (FC_READ_INPUT, 0x0000, 64, "measurement"),
        (FC_READ_INPUT, 0x0083, 60, "measurement"),
        (FC_READ_INPUT, 0x0425, 35, "fixed_settings"),
        (FC_READ_INPUT, 0x0448, 34, "fixed_settings"),
        (FC_READ_INPUT, 0x03E8, 49, "service"),
        (FC_READ_INPUT, 0x047A, 3, "type_code_ext"),
        (FC_READ_INPUT, 0x1000, 9, "calibration_log"),
        (FC_READ_HOLDING, 0x0080, 44, "user_settings"),
        (FC_WRITE_SINGLE, 0x009D, 1, "user_settings"),
        (FC_WRITE_SINGLE, 0x07D2, 1, "commands"),
    ],
)
def test_design_blocks_lie_in_one_region(fc: int, address: int, count: int, name: str) -> None:
    region = ZP_REGIONS.region_for(fc, address, count)
    assert region is not None
    assert region.name == name


@pytest.mark.parametrize(
    ("fc", "address", "count"),
    [
        (FC_READ_INPUT, 0x00C1, 2),  # crosses the end of the measurement region
        (FC_READ_INPUT, 0x00C2, 1),  # starts outside the map
        (FC_READ_INPUT, 0x0418, 14),  # service into fixed settings: separate regions
        (FC_READ_HOLDING, 0x00AB, 2),
        (FC_WRITE_SINGLE, 0x009E, 1),  # above the FC06 bound
        (FC_READ_HOLDING, 0x07D0, 1),  # command registers are write-only
    ],
)
def test_blocks_outside_or_across_regions(fc: int, address: int, count: int) -> None:
    assert ZP_REGIONS.region_for(fc, address, count) is None


def test_documented_map_excludes_observed_regions() -> None:
    assert DOCUMENTED_REGIONS.region_for(FC_READ_INPUT, 0x03E8) is None
    assert all(r.evidence is Evidence.DOCUMENTED for r in DOCUMENTED_REGIONS.regions)
    service = ZP_REGIONS.region_for(FC_READ_INPUT, 0x03E8)
    assert service is not None
    assert service.evidence is Evidence.OBSERVED


def test_overlapping_regions_are_refused() -> None:
    a = Region("a", FC_READ_INPUT, 0, 10, Evidence.DOCUMENTED, "")
    b = Region("b", FC_READ_INPUT, 10, 20, Evidence.DOCUMENTED, "")
    with pytest.raises(FujiConfigurationError):
        RegionMap((a, b))
    # The same span under another function code is fine.
    RegionMap((a, Region("c", FC_READ_HOLDING, 0, 10, Evidence.DOCUMENTED, "")))


def test_region_bounds() -> None:
    with pytest.raises(FujiConfigurationError):
        Region("bad", FC_READ_INPUT, 5, 4, Evidence.DOCUMENTED, "")
    region = Region("r", FC_READ_INPUT, 0x10, 0x1F, Evidence.DOCUMENTED, "")
    assert region.count == 16
    assert region.contains(0x10, 16)
    assert not region.contains(0x10, 17)
    assert not region.contains(0x10, 0)
    assert [r.first for r in ZP_REGIONS.for_function(FC_READ_INPUT)] == sorted(
        r.first for r in ZP_REGIONS.for_function(FC_READ_INPUT)
    )


def test_register_tables() -> None:
    assert RegisterTable.HOLDING.read_function == FC_READ_HOLDING
    assert RegisterTable.INPUT.read_function == FC_READ_INPUT
    assert RegisterTable.HOLDING.number_base == 40_001
    assert RegisterTable.INPUT.number_base == 30_001


# --- Write envelope -----------------------------------------------------------------


def test_envelope_is_exactly_the_design() -> None:
    assert [(r.fc, r.first, r.last) for r in WRITE_ENVELOPE] == [
        (FC_WRITE_SINGLE, 0x0000, 0x009D),
        (FC_WRITE_MULTIPLE, 0x0000, 0x00A3),
        (FC_WRITE_SINGLE, 0x07D1, 0x07D4),
    ]


@pytest.mark.parametrize("fc", [0x03, 0x04, FC_WRITE_SINGLE, FC_WRITE_MULTIPLE])
def test_key_simulation_is_never_writable(fc: int) -> None:
    assert not envelope_allows(fc, KEY_SIMULATION_ADDRESS)


@pytest.mark.parametrize("address", range(0x00A4, 0x00AC))
def test_interference_block_is_never_writable(address: int) -> None:
    assert not envelope_allows(FC_WRITE_MULTIPLE, address)
    assert not envelope_allows(FC_WRITE_SINGLE, address)


@pytest.mark.parametrize(
    ("fc", "address", "count", "allowed"),
    [
        (FC_WRITE_SINGLE, 0x009D, 1, True),
        (FC_WRITE_SINGLE, 0x009E, 1, False),
        (FC_WRITE_MULTIPLE, 0x009E, 6, True),
        (FC_WRITE_MULTIPLE, 0x00A0, 5, False),  # runs into 00A4h
        (FC_WRITE_MULTIPLE, 0x0000, 64, True),
        (FC_WRITE_MULTIPLE, 0x0023, 4, True),  # the manual's FC10 example
        (FC_WRITE_SINGLE, 0x07D1, 1, True),
        (FC_WRITE_SINGLE, 0x07D4, 1, True),
        (FC_WRITE_SINGLE, 0x07D5, 1, False),
        (FC_WRITE_MULTIPLE, 0x07D1, 1, False),
        (FC_WRITE_SINGLE, 0x0000, 0, False),
    ],
)
def test_envelope_allows(fc: int, address: int, count: int, *, allowed: bool) -> None:
    assert envelope_allows(fc, address, count) is allowed


def test_check_envelope_raises_with_context() -> None:
    check_envelope(FC_WRITE_SINGLE, 0x0000)
    with pytest.raises(FujiValidationError) as info:
        check_envelope(FC_WRITE_SINGLE, KEY_SIMULATION_ADDRESS)
    assert info.value.context.function_code == FC_WRITE_SINGLE
    assert info.value.context.register_address == KEY_SIMULATION_ADDRESS


def test_operations() -> None:
    assert set(OPERATIONS) == {
        "return_to_measurement",
        "start_auto_calibration",
        "start_auto_zero_calibration",
        "start_blowback",
    }
    for spec in OPERATIONS.values():
        assert envelope_allows(FC_WRITE_SINGLE, spec.address)
        assert spec.address != KEY_SIMULATION_ADDRESS
        assert spec.safety > SafetyTier.READ_ONLY
    assert OPERATIONS["start_auto_calibration"].safety is SafetyTier.DANGEROUS
    assert OPERATIONS["start_auto_zero_calibration"].safety is SafetyTier.DANGEROUS
    assert OPERATIONS["return_to_measurement"].register_number == 42_002


def test_write_policy_does_not_depend_on_the_register_map() -> None:
    code = (
        "import sys, fujilib.registry.write_policy; "
        "print('fujilib.registry.registers' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
