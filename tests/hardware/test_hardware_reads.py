"""The analyzer facade against a connected ZP analyzer (design §7.2, §10).

Read-only: every call here is a read. Gated as every hardware test is
(``conftest.py``). The assertions hold for any ZP analyzer; the procedure
and what the bench unit reported are in ``docs/hardware-test-day.md`` and the
protocol findings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fujilib import (
    Availability,
    Capability,
    DeviceHealth,
    FujiCapabilityError,
    FujiModbusTimeoutError,
    LabelSource,
    ReadingState,
    open_device,
)
from fujilib.config import DEFAULTS
from fujilib.registry.channels import MEASURED_CHANNELS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fujilib import Analyzer

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]


@pytest.fixture
async def analyzer(hardware_port: str, hardware_address: int) -> AsyncGenerator[Analyzer]:
    async with await open_device(hardware_port, address=hardware_address) as anz:
        yield anz


def requests(anz: Analyzer) -> int:
    return anz.session.counters.requests


async def test_open_and_identify(analyzer: Analyzer, hardware_address: int) -> None:
    info = analyzer.info
    assert info is not None
    assert info.model.startswith("ZP")
    assert info.serial_number
    assert info.address == hardware_address
    assert info.channels, "no channel read non-zero; assert the channels with channel_map"
    # Every probe is well formed, so each gets a definite answer on a healthy link.
    assert Availability.UNKNOWN not in info.availability.values()
    assert info.health in {DeviceHealth.OK, DeviceHealth.PARTIAL}
    snapshot = await analyzer.snapshot()
    assert snapshot.connected
    assert (snapshot.model, snapshot.serial) == (info.model, info.serial_number)


async def test_an_assertion_labels_a_channel(analyzer: Analyzer) -> None:
    first = analyzer.channels[0]
    gas = first.suggested_gas or "o2"
    info = await analyzer.identify(channel_map={first.channel: gas})
    assert info.channels[0].label_source is LabelSource.ASSERTED
    frame = await analyzer.poll()
    assert frame.readings[0].label_source is LabelSource.ASSERTED


async def test_a_poll_is_two_transactions(analyzer: Analyzer) -> None:
    counters = analyzer.session.counters
    before, retried = counters.requests, counters.retries
    frame = await analyzer.poll()
    assert counters.requests - before == 2 + (counters.retries - retried)
    assert frame.channels == tuple(c.channel for c in analyzer.channels)
    assert frame.analyzer is not None
    assert frame.status_timing is not None
    for timing in (frame.readings_timing, frame.status_timing):
        assert 0 < timing.latency_s < DEFAULTS.request_timeout_s
    assert frame.status_timing.t_request_mono_ns > frame.readings_timing.t_reply_mono_ns
    assert all(r.state is not ReadingState.UNKNOWN for r in frame.readings if r.value is not None)
    assert analyzer.last_frame is frame


async def test_a_poll_without_detail(analyzer: Analyzer) -> None:
    frame = await analyzer.poll(detail=False)
    assert frame.analyzer is None
    assert {r.valid for r in frame.readings} == {None}


async def test_channel_reads_and_status(analyzer: Analyzer) -> None:
    channel = analyzer.channels[0].channel
    reading = await analyzer.read_channel(channel)
    assert reading.channel is channel
    status = await analyzer.status()
    assert len(status.alarms) == 6
    if channel.is_measured:
        assert (await analyzer.channel_status(channel)).range in {1, 2}


async def test_metadata(analyzer: Analyzer) -> None:
    info = analyzer.info
    assert info is not None
    meta = await analyzer.read_metadata()
    assert meta.serial_number == info.serial_number
    assert set(meta.current_range) == set(MEASURED_CHANNELS)
    clock = info.availability[Capability.CLOCK] is Availability.SUPPORTED
    assert (meta.clock is not None) == clock
    assert 0 <= meta.response_time_o2_s <= 60


async def test_ranges_settings_and_parameters(analyzer: Analyzer) -> None:
    info = analyzer.info
    assert info is not None
    assert await analyzer.read_ranges() == info.ranges
    settings = await analyzer.read_settings()
    one = await analyzer.read_parameter("response_time.o2")
    assert one.value == settings["response_time.o2"].value
    several = await analyzer.read_parameters(["response_time.o2", "hold.mode"])
    assert list(several) == ["response_time.o2", "hold.mode"]


async def test_logs(analyzer: Analyzer) -> None:
    info = analyzer.info
    assert info is not None
    assert len(await analyzer.read_error_log()) <= 14
    if info.availability[Capability.CALIBRATION_LOG] is Availability.SUPPORTED:
        assert isinstance(await analyzer.read_calibration_log(), tuple)
    else:
        before = requests(analyzer)
        with pytest.raises(FujiCapabilityError):
            await analyzer.read_calibration_log()
        assert requests(analyzer) == before  # refused before anything was sent


async def test_clock_adc_and_reprobe(analyzer: Analyzer) -> None:
    info = analyzer.info
    assert info is not None
    if info.availability[Capability.CLOCK] is Availability.SUPPORTED:
        assert (await analyzer.read_clock()).clock.year >= 2000
        assert await analyzer.reprobe(Capability.CLOCK) is Availability.SUPPORTED
    if info.availability[Capability.ADC_VALUES] is Availability.SUPPORTED:
        assert len((await analyzer.read_adc()).raw) == 21


async def test_sustained_polls(analyzer: Analyzer) -> None:
    counters = analyzer.session.counters
    # identify()'s probes of absent capabilities were answered with exceptions.
    before, recovered = dict(counters.failures), counters.recovered
    for _ in range(50):
        await analyzer.poll()
    failed = {k: n - before.get(k, 0) for k, n in counters.failures.items() if n > before.get(k, 0)}
    # Background loss is about one request in 3,000 (findings §6.3); a retry
    # recovers it, and nothing else fails.
    assert set(failed) <= {"timeout", "frame"}
    assert counters.recovered - recovered == sum(failed.values())


async def test_a_deadline_bounds_an_operation(analyzer: Analyzer) -> None:
    frame = await analyzer.poll(timeout=1.0)
    assert frame.analyzer is not None


async def test_an_empty_station_times_out_and_releases_the_port(
    hardware_port: str, hardware_address: int, other_address: int
) -> None:
    with pytest.raises(FujiModbusTimeoutError) as caught:
        await open_device(hardware_port, address=other_address)
    assert caught.value.context.address == other_address
    assert caught.value.context.port is not None
    # The failed open closed the port, so it opens again at once.
    async with await open_device(hardware_port, address=hardware_address) as anz:
        assert anz.info is not None


async def test_close_and_open_again(hardware_port: str, hardware_address: int) -> None:
    for _ in range(3):
        async with await open_device(hardware_port, address=hardware_address) as anz:
            await anz.poll()
