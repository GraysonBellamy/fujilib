"""The Analyzer facade against the simulated bench analyzer (design §6, §7.2).

Each operation is checked for the exact transactions it sends, and every
refusal for sending nothing at all.
"""

from __future__ import annotations

import logging

import anyio
import pytest

from fujilib.devices.capability import Availability, Capability
from fujilib.devices.decode import decode_ranges
from fujilib.devices.models import DeviceHealth, Frame, ReadingState
from fujilib.devices.session import SessionState
from fujilib.errors import (
    FujiCapabilityError,
    FujiConnectionError,
    FujiDecodeError,
    FujiFirmwareError,
    FujiModbusTimeoutError,
    FujiProtocolUnsupportedError,
    FujiTimeoutError,
    FujiValidationError,
)
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.registry.channels import MEASURED_CHANNELS, ChannelId, ChannelRole, Gas, LabelSource
from fujilib.registry.enums import CalibrationKind
from fujilib.registry.registers import REGISTRY
from fujilib.registry.units import Unit
from fujilib.testing import FaultKind
from tests.facade import (
    CLOCK,
    FC03,
    FC04,
    IDENTIFY,
    METADATA,
    POLL,
    PROBES,
    RANGES,
    SETTINGS,
    analyzer_on,
    bench,
    documented_only,
    newer_firmware,
    when,
)
from tests.factories import bench_banks

pytestmark = pytest.mark.anyio

_, INPUT = bench_banks()
CH1, CH2, CH3, CH4, CH6 = (
    ChannelId.CH1,
    ChannelId.CH2,
    ChannelId.CH3,
    ChannelId.CH4,
    ChannelId.CH6,
)


# --- Identity ------------------------------------------------------------------------------


async def test_identify_the_bench_unit() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False) as (anz, _line):
        info = await anz.identify()
        assert mock.transactions() == IDENTIFY + PROBES
        assert anz.info == info
    assert info.model == "ZPA"
    assert info.serial_number == "N8A0259T"
    assert info.type_code.raw.startswith("ZPACBJY1MPF")
    assert [c.channel for c in info.channels] == [CH1, CH2, CH3]
    assert [c.gas for c in info.channels] == [Gas.CO2, Gas.CO, Gas.O2]
    assert {c.label_source for c in info.channels} == {LabelSource.ASSERTED}
    assert info.ranges == decode_ranges(INPUT)
    assert info.capabilities == Capability.CLOCK | Capability.ADC_VALUES
    assert info.availability[Capability.CALIBRATION_LOG] is Availability.UNSUPPORTED
    assert info.health is DeviceHealth.OK
    assert info.address == 1
    assert info.protocol is ProtocolKind.MODBUS_RTU
    assert info.serial_settings.baudrate == 38_400
    assert info.firmware is None


async def test_identify_without_an_assertion_only_suggests_labels() -> None:
    async with analyzer_on(bench(), channel_map=None) as (anz, _line):
        channels = anz.channels
    assert [c.gas for c in channels] == [Gas.UNKNOWN] * 3
    assert [c.suggested_gas for c in channels] == [Gas.CO2, Gas.CO, Gas.O2]
    assert channels[2].label_source is LabelSource.INFERRED


async def test_identify_replaces_the_asserted_map() -> None:
    async with analyzer_on(bench()) as (anz, _line):
        info = await anz.identify(channel_map={"ch3": "O2"})
        assert dict(anz.session.asserted) == {CH3: Gas.O2}
    assert [c.label_source for c in info.channels] == [
        LabelSource.TYPE_CODE,
        LabelSource.TYPE_CODE,
        LabelSource.ASSERTED,
    ]
    assert info.channels[0].gas is Gas.UNKNOWN


async def test_an_assertion_the_type_code_contradicts_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="fujilib.session"):
        async with analyzer_on(bench(), channel_map={CH1: Gas.CO}) as (anz, _line):
            await anz.identify()
    warnings = [r for r in caplog.records if "suggests" in r.getMessage()]
    assert len(warnings) == 1
    assert "CH1 is asserted as co, but the type code suggests co2" in warnings[0].getMessage()


async def test_a_station_that_is_not_a_zp_analyzer_is_refused() -> None:
    mock = bench()
    mock.set_register("identity.type_code", encode_chars("XYZ", 26))
    async with analyzer_on(mock, identify=False) as (anz, _line):
        with pytest.raises(FujiProtocolUnsupportedError) as caught:
            await anz.identify()
        assert anz.info is None
    context = caught.value.context
    assert (context.port, context.address, context.command_name) == ("mock://zp", 1, "identify")


async def test_a_probe_without_an_answer_leaves_identity_partial() -> None:
    mock = bench()
    mock.inject(FaultKind.DROP, times=None, when=when(FC04, 0x047A))
    async with analyzer_on(mock, identify=False, read_retries=0) as (anz, _line):
        info = await anz.identify()
    assert info.availability[Capability.TYPE_CODE_EXT] is Availability.UNKNOWN
    assert info.health is DeviceHealth.PARTIAL


# --- Measurements --------------------------------------------------------------------------


async def test_poll_is_two_transactions() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        frame = await anz.poll()
        assert anz.last_frame is frame
        assert dict(anz.session.current_ranges or {}) == dict.fromkeys(MEASURED_CHANNELS, 1)
    assert mock.transactions() == POLL
    assert frame.channels == (CH1, CH2, CH3)
    assert [r.state for r in frame.readings] == [ReadingState.OK] * 3
    assert frame.channel("CH3").value == 20.18


async def test_poll_without_detail_is_one_transaction() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        frame = await anz.poll(detail=False)
    assert mock.transactions() == POLL[:1]
    assert {r.valid for r in frame.readings} == {None}


async def test_poll_establishes_channels_without_identify() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False, channel_map=None) as (anz, _line):
        frame = await anz.poll()
        assert anz.info is None
    assert mock.transactions() == POLL
    assert frame.channels == (CH1, CH2, CH3)
    assert {r.label_source for r in frame.readings} == {LabelSource.UNKNOWN}


async def test_a_channel_that_comes_alive_stays_established(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        mock.set_reading(CH4, 5, 1)
        with caplog.at_level(logging.INFO, logger="fujilib.session"):
            alive = await anz.poll()
        mock.set_reading(CH4, 0, 0)
        later = await anz.poll()
        info = anz.info
    assert alive.channels == later.channels == (CH1, CH2, CH3, CH4)
    assert later.channel(CH4).value == 0.0
    assert info is not None
    assert [c.channel for c in info.channels] == [CH1, CH2, CH3, CH4]
    assert "CH4 read non-zero" in caplog.text


async def test_identify_sets_the_range_baseline() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        assert dict(anz.session.current_ranges or {}) == dict.fromkeys(MEASURED_CHANNELS, 1)
        mock.set_register("range.ch3.current", 1)
        await anz.poll()
        await anz.read_metadata()
    assert mock.transactions() == POLL + RANGES + METADATA + CLOCK


async def test_a_replaced_map_keeps_the_channels_it_established() -> None:
    mock = bench()
    async with analyzer_on(mock, channel_map={CH1: Gas.CO2, CH4: Gas.O2}) as (anz, _line):
        assert [c.channel for c in anz.channels] == [CH1, CH2, CH3, CH4]
        info = await anz.identify(channel_map={"CH1": "co2"})
        frame = await anz.poll()
    assert [c.channel for c in info.channels] == [CH1, CH2, CH3, CH4]
    assert frame.channels == (CH1, CH2, CH3, CH4)
    assert frame.channel(CH4).label_source is not LabelSource.ASSERTED


async def test_a_range_change_makes_the_range_tables_stale() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        await anz.poll()
        await anz.read_metadata()
        assert mock.transactions() == POLL + METADATA + CLOCK  # the ranges were cached
        mock.clear()
        mock.set_register("range.ch3.current", 1)
        await anz.poll()
        meta = await anz.read_metadata()
    assert mock.transactions() == POLL + RANGES + METADATA + CLOCK
    assert meta.current_range[CH3] == 2


async def test_read_channel() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        reading = await anz.read_channel("CH3")
    assert mock.transactions() == POLL
    assert reading.gas is Gas.O2
    assert reading.value == 20.18


async def test_read_channel_refuses_an_unestablished_channel_before_io() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiValidationError, match="CH4 is not an established channel"):
            await anz.read_channel(CH4)
    assert mock.transactions() == []


async def test_status_and_channel_status() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        status = await anz.status()
        channel = await anz.channel_status("CH3")
    assert mock.transactions() == POLL + POLL
    assert status.instrument_error is False
    assert len(status.alarms) == 6
    assert channel.range == 1
    assert channel.hold is False


async def test_channel_status_refuses_a_derived_channel_before_io() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiValidationError, match="CH6 is a derived channel"):
            await anz.channel_status(CH6)
    assert mock.transactions() == []


# --- Metadata and ranges -------------------------------------------------------------------


async def test_read_metadata() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        meta = await anz.read_metadata()
    assert mock.transactions() == METADATA + CLOCK
    assert meta.serial_number == "N8A0259T"
    assert meta.response_time_o2_s == 15
    assert meta.response_time_s[CH3] == 15
    assert meta.clock is not None
    assert meta.current_range[CH1] == 1


async def test_read_metadata_identifies_first_when_needed() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False) as (anz, _line):
        meta = await anz.read_metadata()
        assert anz.info is not None
    assert mock.transactions() == IDENTIFY + PROBES + METADATA + CLOCK
    assert meta.serial_number == "N8A0259T"


async def test_read_metadata_probes_a_clock_whose_probe_went_unanswered() -> None:
    mock = bench()
    mock.inject(FaultKind.DROP, times=2, when=when(FC04, 0x03E8))
    async with analyzer_on(mock, identify=False, read_retries=0) as (anz, _line):
        await anz.identify()
        assert anz.session.availability[Capability.CLOCK] is Availability.UNKNOWN
        mock.clear()
        meta = await anz.read_metadata()
        assert anz.session.availability[Capability.CLOCK] is Availability.SUPPORTED
    assert mock.transactions() == CLOCK + METADATA + CLOCK
    assert meta.clock is not None


async def test_read_metadata_leaves_out_a_clock_the_analyzer_lacks() -> None:
    mock = bench(documented_only())
    async with analyzer_on(mock) as (anz, _line):
        meta = await anz.read_metadata()
    assert mock.transactions() == METADATA
    assert meta.clock is None


async def test_read_ranges_refreshes_the_cache() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        mock.set_register("range.ch1.range1.full_scale", 2000)
        ranges = await anz.read_ranges()
        info = anz.info
    assert mock.transactions() == RANGES
    assert ranges[0].full_scale[0] == 20.0
    assert info is not None
    assert info.ranges == ranges


# --- Diagnostics ---------------------------------------------------------------------------


async def test_read_clock_and_adc() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        clock = await anz.read_clock()
        adc = await anz.read_adc()
    assert mock.transactions() == [(FC04, 0x03E8, 7), (FC04, 0x03EF, 42)]
    assert clock.clock.year == 2026
    assert clock.read_at.tzinfo is not None
    assert len(adc.raw) == 21


async def test_an_unsupported_capability_is_refused_before_io() -> None:
    mock = bench(documented_only())
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiCapabilityError, match="clock"):
            await anz.read_clock()
        with pytest.raises(FujiCapabilityError, match="adc_values"):
            await anz.read_adc()
        with pytest.raises(FujiCapabilityError, match="clock"):
            await anz.read_parameter("clock.year")
    assert mock.transactions() == []


async def test_a_register_read_by_name_keeps_availability_current() -> None:
    mock = bench(documented_only())
    async with analyzer_on(mock, identify=False) as (anz, _line):
        with pytest.raises(FujiCapabilityError):
            await anz.read_parameter("clock.year")
        assert anz.session.availability[Capability.CLOCK] is Availability.UNSUPPORTED
    async with analyzer_on(bench(), identify=False) as (anz, _line):
        assert (await anz.read_parameters(["clock.year", "clock.month"]))["clock.month"].value
        assert anz.session.availability[Capability.CLOCK] is Availability.SUPPORTED


async def test_a_capability_found_missing_on_first_use_is_remembered() -> None:
    mock = bench(documented_only())
    async with analyzer_on(mock, identify=False) as (anz, _line):
        with pytest.raises(FujiCapabilityError) as caught:
            await anz.read_clock()
        assert anz.session.availability[Capability.CLOCK] is Availability.UNSUPPORTED
        with pytest.raises(FujiCapabilityError):
            await anz.read_clock()
    assert mock.transactions() == CLOCK  # the second call sent nothing
    assert not isinstance(caught.value, FujiFirmwareError)


async def test_a_clock_that_does_not_decode_is_invalid_data() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        mock.set_register("clock.month", 0x13)
        with pytest.raises(FujiDecodeError) as caught:
            await anz.read_clock()
        assert anz.session.availability[Capability.CLOCK] is Availability.INVALID_DATA
        info = anz.info
        mock.set_register("clock.month", 0x09)
        assert (await anz.read_clock()).clock.month == 9
    context = caught.value.context
    assert (context.port, context.address, context.command_name) == ("mock://zp", 1, "read_clock")
    assert info is not None
    assert info.capabilities == Capability.ADC_VALUES


async def test_reprobe() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        found = await anz.reprobe(Capability.CLOCK)
        with pytest.raises(FujiValidationError, match="not a probed capability"):
            await anz.reprobe(Capability.ALARMS)
    assert found is Availability.SUPPORTED
    assert mock.transactions() == CLOCK


# --- Logs ----------------------------------------------------------------------------------


async def test_read_error_log() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        log = await anz.read_error_log()
    assert mock.transactions() == [(FC04, 0x003D, 60), (FC04, 0x0079, 10), (FC04, 0x003D, 5)]
    assert log


async def test_the_calibration_log_needs_newer_firmware() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiFirmwareError, match="calibration_log"):
            await anz.read_calibration_log()
    assert mock.transactions() == []


async def test_the_calibration_log_of_every_established_channel() -> None:
    mock = bench(newer_firmware())
    async with analyzer_on(mock) as (anz, _line):
        entries = await anz.read_calibration_log()
        one = await anz.read_calibration_log("CH2")
    assert len(mock.transactions()) == 3 * 7 + 7
    assert entries == one
    assert one[0].kind is CalibrationKind.SPAN_RANGE1


async def test_the_calibration_log_is_per_measured_channel() -> None:
    mock = bench(newer_firmware())
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiValidationError, match="CH6 is a derived channel"):
            await anz.read_calibration_log(CH6)
    assert mock.transactions() == []


async def test_a_missing_calibration_log_found_on_first_use() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False) as (anz, _line):
        with pytest.raises(FujiFirmwareError):
            await anz.read_calibration_log(CH1)
        assert anz.session.availability[Capability.CALIBRATION_LOG] is Availability.UNSUPPORTED


# --- Parameters ----------------------------------------------------------------------------


async def test_read_parameter() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        value = await anz.read_parameter("response_time.o2")
    assert mock.transactions() == [(FC03, 0x0053, 1)]
    assert value.value == 15
    assert value.unit == "s"


async def test_a_scaled_parameter_reads_the_ranges_first_when_unknown() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False) as (anz, _line):
        value = await anz.read_parameter("calibration_gas.ch3.range1.span")
        again = await anz.read_parameter("calibration_gas.ch3.range1.span")
    span = (FC03, REGISTRY.resolve("calibration_gas.ch3.range1.span").address, 1)
    assert mock.transactions() == [*RANGES, span, span]
    assert value == again
    assert isinstance(value.value, float)
    assert value.unit == Unit.VOL_PERCENT.value


async def test_read_parameters_in_order_with_alarm_targets() -> None:
    names = ["alarm1.range1.high", "response_time.o2"]
    async with analyzer_on(bench()) as (anz, _line):
        raw = await anz.read_parameters(names)
        scaled = await anz.read_parameters(names, alarm_targets={1: "CH1"})
    assert list(raw) == list(scaled) == names
    assert raw["alarm1.range1.high"].value is None
    assert isinstance(scaled["alarm1.range1.high"].value, float)


@pytest.mark.parametrize(
    ("names", "targets", "match"),
    [
        ("response_time.o2", None, "not one string"),
        (["no.such.register"], None, "unknown register"),
        (["response_time.o2"], {7: "CH1"}, "alarm numbers are 1-6"),
        (["response_time.o2"], {1: "CH13"}, "unknown channel"),
    ],
)
async def test_parameters_are_checked_before_io(
    names: object, targets: dict[int, str] | None, match: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiValidationError, match=match):
            await anz.read_parameters(names, alarm_targets=targets)  # type: ignore[arg-type]
    assert mock.transactions() == []


async def test_read_settings() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        settings = await anz.read_settings(alarm_targets={1: CH1})
    assert mock.transactions() == SETTINGS
    assert settings["response_time.o2"].value == 15
    assert isinstance(settings["calibration_gas.ch3.range1.span"].value, float)


# --- The choke point -----------------------------------------------------------------------


async def test_a_closed_analyzer_refuses_every_call_before_io() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        await anz.close()
        await anz.close()
        assert anz.session.state is SessionState.CLOSED
        with pytest.raises(FujiConnectionError, match="closed"):
            await anz.poll()
        snap = await anz.snapshot()
    assert mock.transactions() == []
    assert snap.connected is False


async def test_a_connection_failure_breaks_the_session() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        await anz.session._port.transport.aclose()
        with pytest.raises(FujiConnectionError):
            await anz.poll()
        assert anz.session.state is SessionState.BROKEN
        with pytest.raises(FujiConnectionError, match="open the analyzer again"):
            await anz.status()
        snap = await anz.snapshot()
    assert snap.connected is False
    assert snap.last_error is not None
    assert snap.last_error.command_name == "poll"


async def test_a_failure_is_kept_as_the_last_error() -> None:
    mock = bench()
    mock.inject(FaultKind.DROP, times=None)
    async with analyzer_on(mock, identify=False, read_retries=0, request_timeout=0.05) as (
        anz,
        _line,
    ):
        with pytest.raises(FujiModbusTimeoutError):
            await anz.poll()
        assert anz.session.state is SessionState.OPEN
        last = anz.session.last_error
    assert last is not None
    assert (last.port, last.address, last.command_name) == ("mock://zp", 1, "poll")


async def test_an_expired_deadline_sends_nothing() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        with pytest.raises(FujiTimeoutError) as caught:
            await anz.poll(timeout=0)
    assert mock.transactions() == []
    context = caught.value.context
    assert (context.port, context.command_name) == ("mock://zp", "poll")


async def test_the_deadline_covers_the_wait_for_the_port() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        release = anyio.Event()

        async def hold_the_port() -> None:
            async with anz.session._port.lock:
                await release.wait()

        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(hold_the_port)
            await anyio.sleep(0.01)
            with pytest.raises(FujiTimeoutError):
                await anz.poll(timeout=0.05)
            release.set()
    assert mock.transactions() == []


async def test_concurrent_operations_do_not_interleave() -> None:
    # Each is several client calls, which only the session's lock holds together.
    mock = bench()
    log = [(FC04, 0x003D, 60), (FC04, 0x0079, 10), (FC04, 0x003D, 5)]
    async with analyzer_on(mock) as (anz, _line):
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(anz.identify)
            _ = tg.start_soon(anz.read_error_log)
    transactions = mock.transactions()
    assert transactions in (IDENTIFY + PROBES + log, log + IDENTIFY + PROBES)


async def test_a_second_close_waits_for_the_operation_in_progress() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        mock.inject(FaultKind.DELAY, delay_s=0.05)
        closed: list[bool] = []

        async def close_twice() -> None:
            await anyio.sleep(0.01)
            async with anyio.create_task_group() as inner:
                _ = inner.start_soon(anz.close)
                await anyio.sleep(0.001)
                await anz.close()
                closed.append(anz.session._port.closed)

        frames: list[Frame] = []
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(close_twice)
            frames.append(await anz.poll())
    assert frames[0].analyzer is not None
    assert closed == [True]


async def test_a_call_refused_while_queued_is_not_the_last_error() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        mock.inject(FaultKind.DELAY, delay_s=0.05)
        refused: list[BaseException] = []

        async def queued() -> None:
            await anyio.sleep(0.01)
            try:
                await anz.read_ranges()
            except FujiConnectionError as exc:
                refused.append(exc)

        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(queued)
            await anz.poll()
            await anz.close()
        last = anz.session.last_error
    assert len(refused) == 1
    assert "closed" in str(refused[0])
    assert last is None


async def test_invalid_timeouts_are_refused() -> None:
    async with analyzer_on(bench()) as (anz, _line):
        with pytest.raises(FujiValidationError, match="timeout"):
            await anz.poll(timeout=-1)


# --- Snapshots and properties --------------------------------------------------------------


async def test_snapshot_needs_no_io() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False) as (anz, _line):
        before = await anz.snapshot()
        await anz.identify()
        mock.clear()
        after = await anz.snapshot(name="zpa")
        assert mock.transactions() == []
    assert (before.name, before.model, before.serial, before.type_code) == (
        "analyzer",
        None,
        None,
        None,
    )
    assert before.channels == (CH1, CH2, CH3)
    assert before.connected is True
    assert after.name == "zpa"
    assert (after.model, after.serial) == ("ZPA", "N8A0259T")
    assert after.type_code is not None
    assert after.capabilities == Capability.CLOCK | Capability.ADC_VALUES
    assert after.captured_at.tzinfo is not None


async def test_a_recovered_read_is_counted() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        mock.inject(FaultKind.DROP)
        await anz.poll()
        snap = await anz.snapshot()
        counters = anz.session.counters
    assert snap.recoverable_error_count == 1
    assert counters.retries == 1


async def test_properties() -> None:
    async with analyzer_on(bench()) as (anz, _line):
        assert anz.address == 1
        assert anz.port == "mock://zp"
        assert anz.protocol is ProtocolKind.MODBUS_RTU
        assert repr(anz) == "<Analyzer ZPA on mock://zp station 1>"
        assert repr(anz.session) == "<Session mock://zp station 1 open>"
        assert anz.session.profile.name == "zp"
        assert anz.session.serial_settings.port == "mock://zp"
        assert anz.session.ranges == decode_ranges(INPUT)
        assert anz.channels[2].role is ChannelRole.INSTANTANEOUS
    async with analyzer_on(bench(), identify=False) as (anz, _line):
        assert repr(anz) == "<Analyzer unidentified on mock://zp station 1>"
        assert anz.session.ranges is None
        assert anz.session.current_ranges is None


async def test_the_analyzer_is_its_own_context_manager() -> None:
    async with analyzer_on(bench()) as (anz, _line):
        async with anz as same:
            assert same is anz
        assert anz.session.state is SessionState.CLOSED
