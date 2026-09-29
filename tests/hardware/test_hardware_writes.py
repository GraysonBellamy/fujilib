"""Setting writes and commands against a connected ZP analyzer (design §6, §10).

**These change the analyzer.** Marked ``hardware_stateful``: they run only with
``FUJILIB_ENABLE_STATEFUL_TESTS=1`` as well as the port, and each session needs
the owner's authorization. The procedure is in ``docs/hardware-test-day.md``.

Every test restores what it wrote, and the ``analyzer`` fixture reads every
writable setting first and checks, at the end, that the analyzer has them all
back; if not, it writes the words it read back and fails the test. The first tests write only
registers of channels the bench analyzer does not have (Ch4 and Ch5), which
change no measurement. Nothing here starts a calibration: the bench analyzer
has no calibration valves to drive (design §2.6).

Run on asyncio only, so each write is made once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from fujilib import (
    Capability,
    ChannelId,
    FujiCapabilityError,
    FujiConfirmationRequiredError,
    open_device,
)
from fujilib.devices.operations import CommandOutcome
from fujilib.devices.settings import SETTINGS_FORMAT, ApplyStatus
from fujilib.devices.writes import WriteState
from fujilib.registry.enums import HoldMode
from fujilib.registry.registers import REGISTRY
from fujilib.registry.write_policy import OPERATIONS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fujilib import Analyzer

pytestmark = [pytest.mark.hardware_stateful, pytest.mark.anyio]

CH3 = ChannelId.CH3
WRITABLE = tuple(s.name for s in REGISTRY if s.writable)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def analyzer(hardware_port: str, hardware_address: int) -> AsyncGenerator[Analyzer]:
    async with await open_device(
        hardware_port, address=hardware_address, channel_map={"CH3": "o2"}
    ) as anz:
        baseline = {n: v.raw for n, v in (await anz.read_parameters(WRITABLE)).items()}
        try:
            yield anz
        finally:
            after = {n: v.raw for n, v in (await anz.read_parameters(WRITABLE)).items()}
            changed = {n: (baseline[n], after[n]) for n in WRITABLE if after[n] != baseline[n]}
            if changed:
                await _restore(anz, changed)
            assert not changed, f"settings left changed, now restored: {changed}"


async def _restore(anz: Analyzer, changed: dict[str, tuple[Any, Any]]) -> None:
    """Write back the words read before, through the client (which checks the envelope)."""
    client = anz.session._client
    for name, (before, _after) in changed.items():
        await client.write_register(REGISTRY.resolve(name).address, int(before))


async def _write_and_restore(anz: Analyzer, name: str, value: object, **kw: Any) -> None:
    before = await anz.read_parameter(name)
    try:
        result = await anz.write_parameter(name, value, confirm=True, **kw)
        assert result.state is WriteState.VERIFIED
        assert result.acknowledged
        assert result.previous.raw == before.raw
        assert (await anz.read_parameter(name)).raw == result.requested.raw
    finally:
        await anz.write_parameter(name, before.value, confirm=True, **kw)
    assert (await anz.read_parameter(name)).raw == before.raw


# --- Registers of absent channels: nothing measured changes ------------------------------------


async def test_a_hold_value_of_an_absent_channel(analyzer: Analyzer) -> None:
    before = (await analyzer.read_parameter("hold.ch5.value")).raw
    assert isinstance(before, int)
    await _write_and_restore(analyzer, "hold.ch5.value", (before + 37) % 101)


async def test_a_response_time_of_an_absent_component(analyzer: Analyzer) -> None:
    before = (await analyzer.read_parameter("response_time.ndir4")).raw
    assert isinstance(before, int)
    await _write_and_restore(analyzer, "response_time.ndir4", 16 if before != 16 else 17)


async def test_a_calibration_gas_of_an_absent_channel(analyzer: Analyzer) -> None:
    gas = await analyzer.read_parameter("calibration_gas.ch5.range1.span")
    assert isinstance(gas.value, float)
    assert gas.unit is not None
    wanted = gas.value - 0.01 if gas.value > 1 else gas.value + 0.01
    await _write_and_restore(
        analyzer, "calibration_gas.ch5.range1.span", round(wanted, 2), unit=gas.unit
    )


async def test_both_write_functions(analyzer: Analyzer) -> None:
    """FC06 and FC10 with one word write the same register the same way."""
    spec = REGISTRY.resolve("hold.ch4.value")
    before = (await analyzer.read_parameter(spec.name)).raw
    assert isinstance(before, int)
    client = analyzer.session._client
    try:
        await client.write_registers(spec.address, [(before + 11) % 101])
        assert (await analyzer.read_parameter(spec.name)).raw == (before + 11) % 101
        await client.write_register(spec.address, (before + 22) % 101)
        assert (await analyzer.read_parameter(spec.name)).raw == (before + 22) % 101
    finally:
        await client.write_register(spec.address, before)
    assert (await analyzer.read_parameter(spec.name)).raw == before


# --- Settings that change what is measured, each restored --------------------------------------


async def test_the_o2_response_time(analyzer: Analyzer) -> None:
    before = (await analyzer.read_parameter("response_time.o2")).raw
    assert isinstance(before, int)
    await _write_and_restore(analyzer, "response_time.o2", before + 1 if before < 60 else 59)


async def test_output_hold_and_hold_mode(analyzer: Analyzer) -> None:
    hold = (await analyzer.read_parameter("output_hold.enabled")).value
    await _write_and_restore(analyzer, "output_hold.enabled", not hold)
    mode = (await analyzer.read_parameter("hold.mode")).value
    other = HoldMode.SETTING if mode is not HoldMode.SETTING else HoldMode.LAST_VALUE
    await _write_and_restore(analyzer, "hold.mode", other)


async def test_selecting_the_other_range(analyzer: Analyzer) -> None:
    status = await analyzer.channel_status(CH3)
    ranges = {r.channel: r for r in await analyzer.read_ranges()}
    if ranges[CH3].count < 2:
        pytest.skip("CH3 has one range")
    other = 2 if status.range == 1 else 1
    try:
        result = await analyzer.set_range(CH3, other, confirm=True)
        assert result.verified
        assert (await analyzer.channel_status(CH3)).range == other
        frame = await analyzer.poll()
        _unit, _full_scale, decimals = ranges[CH3].of(other)
        assert frame.channel(CH3).decimals == decimals
    finally:
        await analyzer.set_range(CH3, status.range, confirm=True)
    assert (await analyzer.channel_status(CH3)).range == status.range


# --- A document, and the gates ------------------------------------------------------------------


async def test_applying_a_document_and_its_baseline(analyzer: Analyzer) -> None:
    names = ("hold.ch5.value", "response_time.ndir4")
    before = {n: v.raw for n, v in (await analyzer.read_parameters(names)).items()}
    changed = {
        "hold.ch5.value": (int(before["hold.ch5.value"]) + 5) % 101,
        "response_time.ndir4": 20,
    }
    baseline = {"format": SETTINGS_FORMAT, "settings": before}
    try:
        report = await analyzer.apply_settings(
            {"format": SETTINGS_FORMAT, "settings": changed}, confirm=True
        )
        assert report.status is ApplyStatus.OK
    finally:
        restored = await analyzer.apply_settings(baseline, confirm=True)
    assert restored.status is ApplyStatus.OK
    assert not (await analyzer.diff_settings(baseline)).writes


async def test_refusals_send_nothing(analyzer: Analyzer) -> None:
    """The gates, checked without any I/O: no calibration command is ever built here."""
    assert analyzer.info is not None
    assert analyzer.info.model == "ZPA"
    assert Capability.AUTO_CALIBRATION not in analyzer.options
    sent = analyzer.session.counters.requests
    with pytest.raises(FujiConfirmationRequiredError):
        await analyzer.write_parameter("hold.ch5.value", 1)
    for name in ("start_auto_calibration", "start_auto_zero_calibration", "start_blowback"):
        spec = OPERATIONS[name]
        with pytest.raises(FujiCapabilityError):
            analyzer.session.gate(name, tier=spec.safety, confirm=True, requires=spec.requires)
    assert analyzer.session.counters.requests == sent


async def test_return_to_measurement(analyzer: Analyzer) -> None:
    result = await analyzer.return_to_measurement(confirm=True)
    assert result.outcome is CommandOutcome.DONE
    assert result.acknowledged
