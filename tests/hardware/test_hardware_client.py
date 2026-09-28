"""The transport, client and read procedures against a connected ZP analyzer (design §10).

Read-only: nothing here writes a register or sends a command. Gated as every
hardware test is (``conftest.py``). The assertions hold for any ZP analyzer;
what one particular unit reports is recorded in the protocol findings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from fujilib.config import DEFAULTS
from fujilib.devices import reads
from fujilib.devices.capability import Availability, Capability
from fujilib.devices.decode import label_channels
from fujilib.errors import FujiModbusIllegalDataAddressError
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.protocol.modbus.read_plan import BlockRead
from fujilib.registry.channels import MEASURED_CHANNELS
from fujilib.transport.base import SerialSettings
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fujilib.protocol.modbus.client import ModbusClient

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]

FC04 = 0x04


@pytest.fixture
async def client(hardware_port: str, hardware_address: int) -> AsyncGenerator[ModbusClient]:
    async with (
        await SerialTransport.open(SerialSettings(port=hardware_port)) as transport,
        ModbusPort(transport) as port,
    ):
        yield port.client(hardware_address)


async def test_identify(client: ModbusClient) -> None:
    identity = await reads.read_identity(client)
    assert identity.type_code.raw.startswith("ZP")
    assert identity.serial_number
    assert [r.channel for r in identity.ranges] == list(MEASURED_CHANNELS)
    # Every probe is well formed, so each gets a definite answer on a healthy link.
    assert Availability.UNKNOWN not in identity.availability.values()
    assert client.counters.retries == 0


async def test_a_poll_is_two_transactions_timed_after_the_gap(client: ModbusClient) -> None:
    identity = await reads.read_identity(client, probe=False)
    channels = label_channels(identity.nonzero, type_code=identity.type_code)
    frame = await reads.read_frame(client, channels)
    assert frame.status_timing is not None
    assert frame.analyzer is not None
    assert [r.channel for r in frame.readings] == [c.channel for c in channels]
    for timing in (frame.readings_timing, frame.status_timing):
        assert 0 < timing.latency_s < DEFAULTS.request_timeout_s
    between = frame.status_timing.t_request_mono_ns - frame.readings_timing.t_reply_mono_ns
    assert between >= (DEFAULTS.inter_frame_idle_s - 0.0005) * 1e9
    assert len(frame.raw) == 2 * (61 + 60)


async def test_a_poll_without_detail_reads_one_block(client: ModbusClient) -> None:
    identity = await reads.read_identity(client, probe=False)
    channels = label_channels(identity.nonzero, type_code=identity.type_code)
    before = client.counters.requests
    frame = await reads.read_frame(client, channels, detail=False)
    assert client.counters.requests - before == 1
    assert {r.valid for r in frame.readings} <= {None}


async def test_status_ranges_metadata_and_settings(client: ModbusClient) -> None:
    identity = await reads.read_identity(client)
    status = await reads.read_status(client)
    assert set(status.channels) == set(MEASURED_CHANNELS)
    ranges = await reads.read_ranges(client)
    assert ranges == identity.ranges
    channels = label_channels(identity.nonzero, type_code=identity.type_code)
    clock = identity.availability.get(Capability.CLOCK) is Availability.SUPPORTED
    meta = await reads.read_metadata(
        client,
        serial_number=identity.serial_number,
        ranges=ranges,
        channels=channels,
        clock=clock,
    )
    assert meta.serial_number == identity.serial_number
    assert dict(meta.current_range) == {c: s.range for c, s in status.channels.items()}
    assert (meta.clock is not None) == clock
    settings = await reads.read_settings(client, ranges=ranges)
    assert "response_time.o2" in settings


async def test_logs(client: ModbusClient) -> None:
    identity = await reads.read_identity(client)
    log = await reads.read_error_log(client)
    assert len(log) <= 14
    if identity.availability[Capability.CALIBRATION_LOG] is Availability.SUPPORTED:
        assert isinstance(await reads.read_calibration_log(client, "CH1"), tuple)
    else:
        with pytest.raises(FujiModbusIllegalDataAddressError):
            await reads.read_calibration_log(client, "CH1")


async def test_clock_and_adc(client: ModbusClient) -> None:
    identity = await reads.read_identity(client)
    if identity.availability[Capability.CLOCK] is Availability.SUPPORTED:
        reading = await reads.read_clock(client)
        assert reading.clock.year >= 2000
    if identity.availability[Capability.ADC_VALUES] is Availability.SUPPORTED:
        adc = await reads.read_adc(client)
        assert len(adc.raw) == 21


async def test_sustained_polls(client: ModbusClient) -> None:
    identity = await reads.read_identity(client, probe=False)
    channels = label_channels(identity.nonzero, type_code=identity.type_code)
    for _ in range(50):
        await reads.read_frame(client, channels)
    # Background loss is about one request in 3,000 (findings §6.3); any that
    # happened was recovered by a retry, and nothing else failed.
    assert set(client.counters.failures) <= {"timeout", "frame"}
    assert client.counters.recovered == sum(client.counters.failures.values())


async def test_a_cancelled_read_does_not_disturb_the_next(client: ModbusClient) -> None:
    # Design §4.2 on the wire: cancel a read while its reply is on the line, then
    # read another block of the same length at once.
    a, b = BlockRead(FC04, 0x0000, 36), BlockRead(FC04, 0x0083, 36)
    for _ in range(5):
        reference = await client.read(b)
        await anyio.sleep(0.04)  # past the inter-frame gap, so A is sent at once
        with anyio.move_on_after(0.015):
            await client.read(a)
        again = await client.read(b)
        assert again.words == reference.words
        assert again.attempts == 1
