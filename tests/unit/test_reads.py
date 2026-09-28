"""Read procedures against the simulated bench analyzer (design §4.3, §6.6).

Two things are checked for every procedure: the exact transactions it puts
on the wire, and that what it returns equals decoding the committed bench
bank directly, so the path through the client and the simulator adds nothing
and loses nothing.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from fujilib.devices import reads
from fujilib.devices.capability import Availability, Capability
from fujilib.devices.decode import (
    decode_adc,
    decode_analyzer_status,
    decode_calibration_log,
    decode_channel_status,
    decode_clock,
    decode_current_ranges,
    decode_error_log,
    decode_frame,
    decode_identity,
    decode_metadata,
    decode_ranges,
    decode_register,
    label_channels,
    nonzero_channels,
    range_scaling,
    words_of,
)
from fujilib.devices.models import ReadingState
from fujilib.errors import (
    FujiDecodeError,
    FujiModbusError,
    FujiModbusIllegalDataAddressError,
    FujiModbusIllegalDataValueError,
    FujiModbusTimeoutError,
    FujiValidationError,
)
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.registry.channels import MEASURED_CHANNELS, ChannelId, Gas
from fujilib.registry.regions import RegisterTable
from fujilib.registry.registers import CALIBRATION_LOG, ERROR_LOG, REGISTRY
from fujilib.testing import (
    DEFAULT_ZPA_BANK,
    FaultKind,
    MockAnalyzer,
    MockAnalyzerConfig,
    MockRequest,
    mock_analyzer_pair,
    zp_readable_regions,
)
from tests.factories import bench_banks

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.anyio

FC03, FC04 = 0x03, 0x04
HOLDING, INPUT = bench_banks()
ASSERTED = {ChannelId.CH1: Gas.CO2, ChannelId.CH2: Gas.CO, ChannelId.CH3: Gas.O2}
# A request timeout long enough that a stall under load never forces a retry,
# since these tests count transactions exactly.
FAST: dict[str, Any] = {"inter_frame_idle": 0.0, "request_timeout": 0.25, "resync_window": 0.01}
POLL = [(FC04, 0x0000, 61), (FC04, 0x0083, 60)]
IDENTIFY = [(FC04, 0x0425, 35), (FC04, 0x0448, 34), (FC04, 0x0000, 42)]
PROBES = [(FC04, 0x03E8, 49), (FC04, 0x047A, 3), (FC04, 0x1000, 9)]


def pair(config: MockAnalyzerConfig | None = None, **overrides: Any) -> Any:
    return mock_analyzer_pair(config, **{**FAST, **overrides})


def channels() -> tuple[Any, ...]:
    type_code, _ = decode_identity(INPUT)
    return label_channels(nonzero_channels(INPUT), asserted=ASSERTED, type_code=type_code)


def newer_firmware() -> MockAnalyzerConfig:
    """The bench bank on firmware 2.24: digits 27-29 and a calibration log."""
    config = replace(DEFAULT_ZPA_BANK, regions=zp_readable_regions(firmware_2_24=True))
    words = dict(config.input)
    words.update(zip(range(0x047A, 0x047D), encode_chars("ABC", 3), strict=True))
    for channel in range(1, 6):
        for index in range(CALIBRATION_LOG.records):
            words[CALIBRATION_LOG.record_address(channel, index)] = 0xFFFF
    record = (2, 1, 0x86A0, 0x0001, 15, 9, 28, 14, 30)  # CH2 span 1, 100000 counts, 1.5 %FS
    base = CALIBRATION_LOG.record_address(2, 0)
    words.update(zip(range(base, base + 9), record, strict=True))
    return replace(config, input=words)


def when(fc: int, address: int, count: int | None = None) -> Callable[[MockRequest], bool]:
    return lambda r: r.function == fc and r.address == address and count in {None, r.count}


# --- Measurements --------------------------------------------------------------------------


async def test_read_frame_is_one_poll() -> None:
    async with pair() as (client, mock):
        frame = await reads.read_frame(client, channels())
    assert mock.transactions() == POLL
    expected = decode_frame(
        INPUT,
        channels(),
        readings_timing=frame.readings_timing,
        status_timing=frame.status_timing,
        raw=frame.raw,
    )
    assert frame == expected
    assert len(frame.raw) == 2 * (61 + 60)
    assert [r.state for r in frame.readings] == [ReadingState.OK] * 3
    assert frame.channel("CH3").value == 20.18


async def test_read_frame_without_detail_reads_one_block() -> None:
    async with pair() as (client, mock):
        frame = await reads.read_frame(client, channels(), detail=False)
    assert mock.transactions() == POLL[:1]
    assert frame.analyzer is None
    assert frame.status_timing is None
    assert {r.state for r in frame.readings} == {ReadingState.UNKNOWN}
    assert {r.valid for r in frame.readings} == {None}


async def test_hold_arriving_between_the_blocks_is_reported() -> None:
    # The status block is read after the concentrations; a hold that starts in
    # between still marks the reading, which is conservative (design §8).
    async with pair() as (client, mock):

        def hold_before_status(request: MockRequest) -> None:
            if request.address == 0x0083:
                mock.set_register("status.ch1.hold", 1)

        mock.on_request = hold_before_status
        frame = await reads.read_frame(client, channels())
    assert frame.channel("CH1").state is ReadingState.HOLD
    assert frame.channel("CH1").valid is False
    assert frame.channel("CH2").state is ReadingState.OK


async def test_a_failed_status_block_fails_the_poll_and_keeps_the_readings() -> None:
    async with pair() as (client, mock):
        mock.inject(FaultKind.DROP, times=None, when=when(FC04, 0x0083))
        with pytest.raises(FujiModbusTimeoutError) as info:
            await reads.read_frame(client, channels())
    ((key, words),) = info.value.context.extra["completed"]
    assert key == POLL[0]
    assert words == words_of(INPUT, 0, 61)


async def test_read_status() -> None:
    async with pair() as (client, mock):
        status = await reads.read_status(client)
    assert mock.transactions() == POLL
    assert status.analyzer == decode_analyzer_status(INPUT)
    assert dict(status.channels) == {c: decode_channel_status(INPUT, c) for c in MEASURED_CHANNELS}
    assert len(status.timings) == 2


# --- Identity and capabilities --------------------------------------------------------------


async def test_identify_the_bench_unit() -> None:
    async with pair() as (client, mock):
        identity = await reads.read_identity(client)
    assert mock.transactions() == IDENTIFY + PROBES
    type_code, serial = decode_identity(INPUT)
    assert identity.type_code == type_code
    assert identity.serial_number == serial == "N8A0259T"
    assert identity.ranges == decode_ranges(INPUT)
    assert identity.nonzero == nonzero_channels(INPUT) == set(ASSERTED)
    assert dict(identity.availability) == {
        Capability.CLOCK: Availability.SUPPORTED,
        Capability.ADC_VALUES: Availability.SUPPORTED,
        Capability.TYPE_CODE_EXT: Availability.UNSUPPORTED,
        Capability.CALIBRATION_LOG: Availability.UNSUPPORTED,
    }
    unsupported = identity.probes[Capability.TYPE_CODE_EXT]
    assert isinstance(unsupported.error, FujiModbusIllegalDataAddressError)
    assert unsupported.words == ()
    assert unsupported.timing is None
    # Three identity blocks and one probe read that served both clock and A/D.
    assert len(identity.timings) == 4


async def test_identify_without_probing() -> None:
    async with pair() as (client, mock):
        identity = await reads.read_identity(client, probe=False)
    assert mock.transactions() == IDENTIFY
    assert dict(identity.probes) == {}
    assert dict(identity.availability) == {}


async def test_identify_newer_firmware_adds_type_code_digits() -> None:
    async with pair(newer_firmware()) as (client, _mock):
        identity = await reads.read_identity(client)
    assert identity.type_code.raw == decode_identity(INPUT)[0].raw + "ABC"
    assert identity.availability[Capability.TYPE_CODE_EXT] is Availability.SUPPORTED
    assert identity.availability[Capability.CALIBRATION_LOG] is Availability.SUPPORTED


async def test_the_clock_and_ad_are_probed_apart_when_the_joint_read_fails() -> None:
    async with pair() as (client, mock):
        mock.inject(FaultKind.DROP, times=None, when=when(FC04, 0x03E8, 49))
        probes = await reads.probe_capabilities(client)
    assert mock.transactions()[-5:] == [
        (FC04, 0x03E8, 49),
        (FC04, 0x03E8, 7),
        (FC04, 0x03EF, 42),
        (FC04, 0x047A, 3),
        (FC04, 0x1000, 9),
    ]
    assert probes[Capability.CLOCK].availability is Availability.SUPPORTED
    assert probes[Capability.ADC_VALUES].availability is Availability.SUPPORTED


async def test_a_documented_only_map_has_neither_clock_nor_ad() -> None:
    config = replace(DEFAULT_ZPA_BANK, regions=zp_readable_regions(observed=False))
    async with pair(config) as (client, mock):
        probes = await reads.probe_capabilities(client)
    assert mock.transactions() == [
        (FC04, 0x03E8, 49),
        (FC04, 0x03E8, 7),
        (FC04, 0x03EF, 42),
        (FC04, 0x047A, 3),
        (FC04, 0x1000, 9),
    ]
    assert {p.availability for p in probes.values()} == {Availability.UNSUPPORTED}


async def test_probing_one_capability() -> None:
    async with pair() as (client, mock):
        probes = await reads.probe_capabilities(client, [Capability.CLOCK, Capability.CLOCK])
        result = await reads.probe_capability(client, Capability.ADC_VALUES)
    assert list(probes) == [Capability.CLOCK]
    assert mock.transactions() == [(FC04, 0x03E8, 7), (FC04, 0x03EF, 42)]
    assert result.availability is Availability.SUPPORTED
    assert len(result.words) == 42
    assert result.timing is not None


@pytest.mark.parametrize(
    ("capability", "address", "words"),
    [
        (Capability.CLOCK, 0x03E9, (0x0013,)),  # month 13
        (Capability.TYPE_CODE_EXT, 0x047A, (0x1234,)),  # not one character
        (Capability.CALIBRATION_LOG, 0x1000, (9,)),  # channel 9
    ],
)
async def test_data_that_does_not_validate(
    capability: Capability, address: int, words: tuple[int, ...]
) -> None:
    mock_config = newer_firmware()
    async with pair(mock_config) as (client, mock):
        mock.set_words(RegisterTable.INPUT, address, words)
        probes = await reads.probe_capabilities(client)
    result = probes[capability]
    assert result.availability is Availability.INVALID_DATA
    assert isinstance(result.error, FujiDecodeError)
    assert result.words
    others = {c: p.availability for c, p in probes.items() if c is not capability}
    assert set(others.values()) == {Availability.SUPPORTED}


async def test_a_calibration_record_validates_the_log() -> None:
    config = newer_firmware()
    words = dict(config.input)
    words.update(zip(range(0x1000, 0x1009), (1, 0, 5, 0, 3, 9, 28, 14, 0), strict=True))
    async with pair(replace(config, input=words)) as (client, _mock):
        result = await reads.probe_capability(client, Capability.CALIBRATION_LOG)
    assert result.availability is Availability.SUPPORTED
    assert result.words[0] == 1


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ({"kind": FaultKind.DROP, "times": None}, FujiModbusTimeoutError),
        ({"kind": FaultKind.EXCEPTION, "exception_code": 3}, FujiModbusIllegalDataValueError),
        ({"kind": FaultKind.EXCEPTION, "exception_code": 4}, FujiModbusError),
    ],
)
async def test_a_probe_that_fails_otherwise_is_unknown(
    fault: dict[str, Any], error: type[Exception]
) -> None:
    async with pair(newer_firmware()) as (client, mock):
        mock.inject(**fault, when=when(FC04, 0x047A))
        result = await reads.probe_capability(client, Capability.TYPE_CODE_EXT)
    assert result.availability is Availability.UNKNOWN
    assert isinstance(result.error, error)


async def test_only_probed_capabilities_can_be_probed() -> None:
    async with pair() as (client, mock):
        with pytest.raises(FujiValidationError, match="not a probed capability"):
            await reads.probe_capability(client, Capability.ALARMS)
        with pytest.raises(FujiValidationError, match="not a probed capability"):
            await reads.probe_capabilities(client, [Capability.CLOCK, Capability.BLOWBACK])
    assert mock.exchanges == []


async def test_read_ranges() -> None:
    async with pair() as (client, mock):
        ranges = await reads.read_ranges(client)
    assert mock.transactions() == IDENTIFY[:1]
    assert ranges == decode_ranges(INPUT)


# --- Settings and metadata ------------------------------------------------------------------


def expected_metadata(**changes: Any) -> Any:
    ranges = decode_ranges(INPUT)
    return decode_metadata(
        HOLDING,
        serial_number="N8A0259T",
        ranges=ranges,
        channels=channels(),
        current_range=decode_current_ranges(INPUT),
        **changes,
    )


async def test_read_metadata_with_the_clock() -> None:
    ranges = decode_ranges(INPUT)
    async with pair() as (client, mock):
        meta = await reads.read_metadata(
            client,
            serial_number="N8A0259T",
            ranges=ranges,
            channels=channels(),
            clock=True,
        )
    assert mock.transactions() == [
        (FC03, 0x0000, 64),
        (FC03, 0x0040, 64),
        (FC03, 0x0080, 36),
        (FC04, 0x0025, 5),
        (FC04, 0x03E8, 7),
    ]
    assert meta.clock == decode_clock(words_of(INPUT, 0x03E8, 7))
    assert meta.clock_read_at is not None
    assert meta == expected_metadata(
        captured_at=meta.captured_at, clock=meta.clock, clock_read_at=meta.clock_read_at
    )


async def test_read_metadata_without_the_clock() -> None:
    ranges = decode_ranges(INPUT)
    async with pair() as (client, mock):
        meta = await reads.read_metadata(
            client,
            serial_number="N8A0259T",
            ranges=ranges,
            channels=channels(),
            clock=False,
        )
    assert len(mock.transactions()) == 4
    assert meta.clock is None
    assert meta == expected_metadata(captured_at=meta.captured_at)


async def test_read_metadata_reads_the_current_range_itself() -> None:
    async with pair() as (client, mock):
        mock.set_register("range.ch3.current", 1)
        meta = await reads.read_metadata(
            client,
            serial_number="N8A0259T",
            ranges=decode_ranges(INPUT),
            channels=channels(),
            clock=False,
        )
    assert meta.current_range[ChannelId.CH3] == 2
    assert meta.current_range[ChannelId.CH1] == 1


async def test_a_clock_that_does_not_decode_is_left_out(caplog: pytest.LogCaptureFixture) -> None:
    ranges = decode_ranges(INPUT)
    async with pair() as (client, mock):
        mock.set_register("clock.month", 0x13)
        with caplog.at_level(logging.WARNING, logger="fujilib.reads"):
            meta = await reads.read_metadata(
                client,
                serial_number="N8A0259T",
                ranges=ranges,
                channels=channels(),
                clock=True,
            )
    assert meta.clock is None
    assert meta.clock_read_at is None
    assert "clock does not decode" in caplog.text


async def test_read_settings_decodes_every_holding_register() -> None:
    ranges = decode_ranges(INPUT)
    async with pair() as (client, mock):
        settings = await reads.read_settings(client, ranges=ranges)
    assert mock.transactions() == [(FC03, 0x0000, 64), (FC03, 0x0040, 64), (FC03, 0x0080, 44)]
    specs = REGISTRY.in_table(RegisterTable.HOLDING)
    assert list(settings) == [s.name for s in specs]
    for spec in specs:
        expected = decode_register(
            spec, words_of(HOLDING, spec.address, spec.count), scaling=range_scaling(spec, ranges)
        )
        assert settings[spec.name] == expected
    span = settings["calibration_gas.ch1.range1.span"]
    assert span.value is not None
    assert span.unit == "vol%"


async def test_read_registers_reads_what_is_asked_in_the_fewest_blocks() -> None:
    names = ["response_time.o2", "status.ch1.hold", "reading.ch3.value", "status.ch1.hold"]
    async with pair() as (client, mock):
        values = await reads.read_registers(client, names)
    assert list(values) == ["response_time.o2", "status.ch1.hold", "reading.ch3.value"]
    assert values["response_time.o2"].value == 15
    assert values["reading.ch3.value"].raw == INPUT[0x0006]
    assert mock.transactions() == [(FC03, 0x0053, 1), (FC04, 0x0006, 1), (FC04, 0x00A7, 1)]


async def test_read_registers_refuses_unknown_names_before_io() -> None:
    async with pair() as (client, mock):
        with pytest.raises(FujiValidationError, match="unknown register"):
            await reads.read_registers(client, ["status.ch9.hold"])
    assert mock.exchanges == []


# --- Logs -----------------------------------------------------------------------------------


ERROR_SCAN = [(FC04, 0x003D, 60), (FC04, 0x0079, 10), (FC04, 0x003D, 5)]


async def test_read_error_log() -> None:
    async with pair() as (client, mock):
        log = await reads.read_error_log(client)
    assert mock.transactions() == ERROR_SCAN
    assert log == decode_error_log(INPUT)
    assert len(log) == 14


def push_error(mock: MockAnalyzer, record: tuple[int, ...]) -> None:
    """A new error: every record moves one place older, the oldest falls off."""
    first = ERROR_LOG.base
    size = ERROR_LOG.record_words * ERROR_LOG.records
    old = [mock.input.get(a, 0) for a in range(first, first + size)]
    mock.set_words(RegisterTable.INPUT, first, (*record, *old[: size - len(record)]))


async def test_an_error_logged_during_the_read_triggers_one_reread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    new = (3, 28, 19, 5, 1)  # error 4 on CH2, day 28, 19:05
    async with pair() as (client, mock):
        pushed = False

        def log_an_error(request: MockRequest) -> None:
            nonlocal pushed
            if request.count == ERROR_LOG.record_words and not pushed:
                pushed = True
                push_error(mock, new)

        mock.on_request = log_an_error
        with caplog.at_level(logging.INFO, logger="fujilib.reads"):
            log = await reads.read_error_log(client)
    assert mock.transactions() == ERROR_SCAN * 2
    assert log[0].channel is ChannelId.CH2
    assert (log[0].at.day, log[0].at.hour, log[0].at.minute) == (28, 19, 5)
    assert log[1:] == decode_error_log(INPUT)[:13]
    assert "gained an entry" in caplog.text


async def test_the_log_is_read_again_only_once() -> None:
    async with pair() as (client, mock):

        def always(request: MockRequest) -> None:
            if request.count == ERROR_LOG.record_words:
                push_error(mock, (0, 1, 2, 3, 0))

        mock.on_request = always
        await reads.read_error_log(client)
    assert mock.transactions() == ERROR_SCAN * 2


async def test_read_calibration_log() -> None:
    config = newer_firmware()
    async with pair(config) as (client, mock):
        entries = await reads.read_calibration_log(client, "CH2")
        empty = await reads.read_calibration_log(client, ChannelId.CH1)
    base = CALIBRATION_LOG.record_address(2, 0)
    assert mock.transactions()[:7] == [
        *((FC04, base + 63 * k, 63) for k in range(5)),
        (FC04, base + 315, 45),
        (FC04, base, 9),
    ]
    dense = {a: config.input.get(a, 0) for a in range(0x1000, 0x1708)}
    assert entries == decode_calibration_log(dense, ChannelId.CH2)
    (entry,) = entries
    assert (entry.range, entry.detector_count, entry.deviation_percent_fs) == (1, 100_000, 1.5)
    assert empty == ()


async def test_the_calibration_log_is_per_measured_channel() -> None:
    async with pair() as (client, mock):
        with pytest.raises(FujiValidationError, match="no calibration log"):
            await reads.read_calibration_log(client, ChannelId.CH6)
    assert mock.exchanges == []


async def test_the_bench_unit_has_no_calibration_log() -> None:
    async with pair() as (client, _mock):
        with pytest.raises(FujiModbusIllegalDataAddressError):
            await reads.read_calibration_log(client, ChannelId.CH1)


# --- Clock and A/D --------------------------------------------------------------------------


async def test_read_clock() -> None:
    async with pair() as (client, mock):
        reading = await reads.read_clock(client)
    assert mock.transactions() == [(FC04, 0x03E8, 7)]
    assert reading.clock == decode_clock(words_of(INPUT, 0x03E8, 7))
    assert reading.read_at == reading.timing.midpoint_utc


async def test_a_clock_that_is_not_a_date_raises() -> None:
    async with pair() as (client, mock):
        mock.set_register("clock.day", 0x32)
        with pytest.raises(FujiDecodeError):
            await reads.read_clock(client)


async def test_read_adc() -> None:
    async with pair() as (client, mock):
        adc = await reads.read_adc(client)
    assert mock.transactions() == [(FC04, 0x03EF, 42)]
    expected = decode_adc(
        words_of(INPUT, 0x03EF, 42), received_at=adc.received_at, t_mono_ns=adc.t_mono_ns
    )
    assert adc == expected
    assert adc.reference_voltage == expected.raw[15]
