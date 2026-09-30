"""The simulated analyzer, checked through plain anymodbus (design §10).

These tests use ``anymodbus`` directly rather than fujilib's client, so the
simulator is checked by code that does not depend on the code it is meant to test.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

import anyio
import anymodbus
import pytest
from anymodbus import Bus, BusConfig, RetryPolicy, TimingConfig
from anymodbus.crc import crc16_modbus_bytes, verify_crc
from anymodbus.framer import encode_adu
from hypothesis import given
from hypothesis import strategies as st

from fujilib.registry.channels import ChannelId
from fujilib.registry.regions import RegisterTable
from fujilib.registry.units import Unit
from fujilib.testing import (
    BENCH_BANK_PATH,
    DEFAULT_ZPA_BANK,
    ExceptionProfile,
    FaultKind,
    MockAnalyzer,
    MockAnalyzerConfig,
    MockCalibration,
    MockLine,
    MockRegion,
    MockRequest,
    MockWriteViolation,
    load_bank,
    mock_transport,
    zp_readable_regions,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from anyserial import SerialPort

pytestmark = pytest.mark.anyio

FC03, FC04, FC06, FC10 = 0x03, 0x04, 0x06, 0x10


@asynccontextmanager
async def raw_bus(*analyzers: MockAnalyzer, timeout: float = 0.1) -> AsyncGenerator[Bus]:
    """A plain anymodbus bus on a line carrying ``analyzers``: no retries, no idle."""
    async with mock_transport(*analyzers) as (transport, _line):
        config = BusConfig(
            request_timeout=timeout,
            retries=RetryPolicy(retries=0),
            timing=TimingConfig(inter_frame_idle=0.0),
        )
        yield Bus(transport.stream, config=config)


def analyzer(profile: ExceptionProfile = ExceptionProfile.BENCH_1_02) -> MockAnalyzer:
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, profile=profile))


def request(fc: int, address: int, count: int, values: tuple[int, ...] = ()) -> MockRequest:
    return MockRequest(1, fc, address, count, values, b"", 0.0)


# --- Configuration and banks ---------------------------------------------------------------


def test_readable_regions() -> None:
    bench = zp_readable_regions()
    assert MockRegion(FC04, 0x03E8, 0x0479) in bench
    assert not any(r.first == 0x1000 for r in bench)
    documented = zp_readable_regions(observed=False, firmware_2_24=True)
    assert MockRegion(FC04, 0x0425, 0x0469) in documented
    assert MockRegion(FC04, 0x047A, 0x047C) in documented
    assert MockRegion(FC04, 0x1000, 0x1707) in documented
    assert MockRegion(FC03, 0x0000, 0x00AB) in documented


def test_the_default_bank_is_the_sanitized_bench_bank() -> None:
    assert DEFAULT_ZPA_BANK.station == 1
    assert DEFAULT_ZPA_BANK.profile is ExceptionProfile.BENCH_1_02
    assert len(DEFAULT_ZPA_BANK.input) == 312
    assert len(DEFAULT_ZPA_BANK.holding) == 172
    assert "bench" in DEFAULT_ZPA_BANK.description
    assert BENCH_BANK_PATH.name == "zpa_bench_documented.json"
    # No factory block made it into the committed bank (design §13.1 #11).
    assert all(a <= 0x00AB for a in DEFAULT_ZPA_BANK.holding)


def test_load_bank_from_a_mapping() -> None:
    config = load_bank(
        {"input": {"0000": 5}, "holding": {"000A": 7}},
        station=4,
        profile=ExceptionProfile.DOCUMENTED,
        regions=(MockRegion(FC04, 0, 9),),
    )
    assert config == MockAnalyzerConfig(
        station=4,
        profile=ExceptionProfile.DOCUMENTED,
        input={0: 5},
        holding={10: 7},
        regions=(MockRegion(FC04, 0, 9),),
    )


def test_load_bank_defaults() -> None:
    config = load_bank({"station": 3})
    assert config.station == 3
    assert dict(config.input) == {}
    assert load_bank({}).station == 1


def test_load_bank_refuses_a_table_that_is_not_a_mapping() -> None:
    with pytest.raises(TypeError, match="input"):
        load_bank({"input": [1, 2]})


def test_an_empty_analyzer() -> None:
    mock = MockAnalyzer()
    assert mock.station == 1
    assert mock.input == {}
    assert mock.config == MockAnalyzerConfig()


# --- Test controls --------------------------------------------------------------------------


def test_set_and_read_registers_by_name() -> None:
    mock = MockAnalyzer()
    mock.set_register("status.ch1.hold", 1)
    assert mock.register("status.ch1.hold") == (1,)
    assert mock.input[0x00A7] == 1
    mock.set_register("identity.serial_number", [0x41] * 8)
    assert mock.register("identity.serial_number") == (0x41,) * 8
    mock.set_register("output_hold.enabled", 1)
    assert mock.register("output_hold.enabled") == (1,)
    with pytest.raises(ValueError, match="1 word"):
        mock.set_register("status.ch1.hold", [1, 2])


def test_set_reading_encodes_a_signed_triple() -> None:
    mock = MockAnalyzer()
    mock.set_reading(ChannelId.CH2, -9, 3, Unit.PPM)
    assert mock.register("reading.ch2.value") == (0xFFF7,)
    assert mock.register("reading.ch2.decimals") == (3,)
    assert mock.register("reading.ch2.unit") == (1,)
    mock.set_reading("CH5", 1200, 2)
    assert mock.register("reading.ch5.unit") == (0,)


def test_set_words_refuses_values_that_are_not_words() -> None:
    mock = MockAnalyzer()
    mock.set_words(RegisterTable.HOLDING, 0, [0, 0xFFFF])
    with pytest.raises(ValueError, match="0-0xFFFF"):
        mock.set_words(RegisterTable.INPUT, 0, [0x10000])


def test_faults_are_used_up_in_order() -> None:
    mock = MockAnalyzer()
    first = mock.inject(FaultKind.DROP)
    second = mock.inject(FaultKind.DELAY, times=2, delay_s=0.5)
    req = request(FC04, 0, 1)
    assert mock.take_fault(req) is first
    assert mock.take_fault(req) is second
    assert mock.take_fault(req) is second
    assert mock.take_fault(req) is None


def test_faults_can_repeat_and_match() -> None:
    mock = MockAnalyzer()
    fault = mock.inject(FaultKind.CORRUPT_CRC, times=None, when=lambda r: r.address == 5)
    assert mock.take_fault(request(FC04, 0, 1)) is None
    for _ in range(3):
        assert mock.take_fault(request(FC04, 5, 1)) is fault
    assert mock.faults == [fault]


def test_line_refuses_two_analyzers_at_one_station() -> None:
    line = MockLine(MockAnalyzer())
    with pytest.raises(ValueError, match="station 1"):
        line.add(MockAnalyzer())
    assert set(line.stations) == {1}


# --- The model the simulator must agree with ------------------------------------------------


def expected_reply(
    regions: tuple[MockRegion, ...], profile: ExceptionProfile, fc: int, address: int, count: int
) -> int | None:
    """The exception code the manual (or the bench) gives, or ``None`` for data."""
    if fc in {1, 2}:
        return 2 if profile is ExceptionProfile.BENCH_1_02 else 1
    if fc not in {FC03, FC04}:
        return 1
    if not 1 <= count <= 64:
        return 3
    starts = [r for r in regions if r.function == fc and r.first <= address <= r.last]
    if not starts:
        return 2
    return 3 if address + count - 1 > starts[0].last else None


@given(
    fc=st.sampled_from([1, 2, FC03, FC04, 0x05, 0x08, 0x11]),
    address=st.integers(0, 0x1800),
    count=st.integers(0, 70),
    profile=st.sampled_from(list(ExceptionProfile)),
    firmware_2_24=st.booleans(),
    observed=st.booleans(),
)
def test_handle_agrees_with_the_model(
    *,
    fc: int,
    address: int,
    count: int,
    profile: ExceptionProfile,
    firmware_2_24: bool,
    observed: bool,
) -> None:
    regions = zp_readable_regions(observed=observed, firmware_2_24=firmware_2_24)
    mock = MockAnalyzer(MockAnalyzerConfig(profile=profile, regions=regions))
    pdu = mock.handle(request(fc, address, count))
    code = expected_reply(regions, profile, fc, address, count)
    if code is None:
        assert pdu[:2] == bytes((fc, 2 * count))
        assert len(pdu) == 2 + 2 * count
    else:
        assert pdu == bytes((fc | 0x80, code))


# --- On the wire ----------------------------------------------------------------------------


async def test_reads_return_the_bank() -> None:
    mock = analyzer()
    async with raw_bus(mock) as bus:
        slave = bus.slave(1)
        assert await slave.read_input_registers(0x000C, count=3) == tuple(
            DEFAULT_ZPA_BANK.input.get(a, 0) for a in range(0x0C, 0x0F)
        )
        assert await slave.read_holding_registers(0x0000, count=4) == tuple(
            DEFAULT_ZPA_BANK.holding[a] for a in range(4)
        )
        # 0419h-0424h are inside the bench's readable block but not in the bank.
        assert await slave.read_input_registers(0x0419, count=2) == (0, 0)
    assert mock.transactions() == [(FC04, 0x0C, 3), (FC03, 0, 4), (FC04, 0x0419, 2)]
    mock.clear()
    assert mock.exchanges == []


@pytest.mark.parametrize(
    ("address", "count", "error"),
    [
        (0x00C2, 1, anymodbus.IllegalDataAddressError),
        (0x00C0, 3, anymodbus.IllegalDataValueError),
        (0x0000, 65, anymodbus.IllegalDataValueError),
        (0x1000, 9, anymodbus.IllegalDataAddressError),
    ],
)
async def test_read_exceptions(address: int, count: int, error: type[Exception]) -> None:
    async with raw_bus(analyzer()) as bus:
        with pytest.raises(error):
            await bus.slave(1).read_input_registers(address, count=count)


@pytest.mark.parametrize(
    ("profile", "error"),
    [
        (ExceptionProfile.BENCH_1_02, anymodbus.IllegalDataAddressError),
        (ExceptionProfile.DOCUMENTED, anymodbus.IllegalFunctionError),
    ],
)
async def test_bit_reads_answer_as_the_profile_says(
    profile: ExceptionProfile, error: type[Exception]
) -> None:
    async with raw_bus(analyzer(profile)) as bus:
        slave = bus.slave(1)
        with pytest.raises(error):
            await slave.read_coils(0, count=1)
        with pytest.raises(error):
            await slave.read_discrete_inputs(0, count=1)


async def test_diagnostics_are_an_illegal_function() -> None:
    async with raw_bus(analyzer()) as bus:
        with pytest.raises(anymodbus.IllegalFunctionError):
            await bus.slave(1).diagnostic_loopback(b"\x12\x34")


async def receive_exactly(stream: SerialPort, n: int) -> bytes:
    data = b""
    with anyio.fail_after(1):
        while len(data) < n:
            data += await stream.receive(n - len(data))
    return data


async def test_an_unknown_function_code_is_framed_by_silence() -> None:
    mock = analyzer()
    async with mock_transport(mock) as (transport, _):
        await transport.stream.send(encode_adu(slave_address=1, pdu=bytes((0x11,))))
        reply = await receive_exactly(transport.stream, 5)
    assert reply == encode_adu(slave_address=1, pdu=bytes((0x91, 0x01)))
    assert mock.transactions() == [(0x11, None, None)]


async def test_a_request_with_a_bad_crc_gets_no_reply() -> None:
    mock = analyzer()
    frame = bytearray(encode_adu(slave_address=1, pdu=b"\x04\x00\x00\x00\x01"))
    frame[-1] ^= 0xFF
    async with mock_transport(mock) as (transport, line):
        await transport.stream.send(bytes(frame))
        with anyio.move_on_after(0.05) as scope:
            await transport.stream.receive()
        assert scope.cancelled_caught
        assert line.exchanges == []  # the line drops it unread, as the analyzer would
    assert mock.exchanges == []


async def test_an_absent_station_is_silent() -> None:
    async with raw_bus(analyzer(), timeout=0.05) as bus:
        with pytest.raises(anymodbus.FrameTimeoutError):
            await bus.slave(7).read_input_registers(0, count=1)


async def test_stations_on_one_line_answer_for_themselves() -> None:
    first = MockAnalyzer(MockAnalyzerConfig(station=1, input={0: 111}))
    second = MockAnalyzer(MockAnalyzerConfig(station=2, input={0: 222}))
    async with raw_bus(first, second) as bus:
        assert await bus.slave(2).read_input_registers(0, count=1) == (222,)
        assert await bus.slave(1).read_input_registers(0, count=1) == (111,)
        assert await bus.slave(2).read_input_registers(0, count=1) == (222,)
    assert first.transactions() == [(FC04, 0, 1)]
    assert second.transactions() == [(FC04, 0, 1)] * 2


# --- Writes ---------------------------------------------------------------------------------


async def test_documented_writes_are_accepted() -> None:
    mock = analyzer()
    async with raw_bus(mock) as bus:
        slave = bus.slave(1)
        await slave.write_register(0x0049, 1)
        await slave.write_registers(0x0023, [5000, 10, 1000, 10])
        await slave.write_register(0x07D2, 1)
        await slave.write_registers(0x009E, [1, 2, 3, 4, 5, 6])
    assert mock.holding[0x0049] == 1
    assert [mock.holding[a] for a in range(0x23, 0x27)] == [5000, 10, 1000, 10]
    assert mock.commands == [(0x07D2, 1)]
    assert mock.transactions()[:2] == [(FC06, 0x0049, 1), (FC10, 0x0023, 4)]


async def test_a_range_selected_under_the_manual_method_becomes_current() -> None:
    mock = analyzer()
    mock.holding[0x6E] = 2  # Ch1 changes range automatically
    async with raw_bus(mock) as bus:
        await bus.slave(1).write_registers(0x0069, [1, 1, 1, 1, 1])
    assert mock.register("range.ch1.current") == (0,)
    assert [mock.register(f"range.ch{c}.current") for c in range(2, 6)] == [(1,)] * 4
    assert mock.range_changes == {}


async def test_a_range_becomes_current_after_the_lag() -> None:
    mock = MockAnalyzer(replace(DEFAULT_ZPA_BANK, range_lag_s=0.1))
    async with raw_bus(mock) as bus:
        slave = bus.slave(1)
        await slave.write_register(0x006B, 1)
        assert await slave.read_input_registers(0x0027, count=1) == (0,)
        assert 3 in mock.range_changes
        await anyio.sleep(0.12)
        assert await slave.read_input_registers(0x0027, count=1) == (1,)
    assert mock.range_changes == {}


def running(mock: MockAnalyzer) -> MockCalibration | None:
    """The calibration running now (read afresh, past any narrowing)."""
    return mock.calibration


def enabled(*channels: int, scale: float = 0.001) -> MockAnalyzer:
    """A station with auto calibration enabled on ``channels``, flow times 100 s each."""
    holding = {0x14 + c - 1: 1 for c in channels}
    holding.update({0x84 + k: 100 for k in range(7)})
    holding[0x68] = 100
    return MockAnalyzer(MockAnalyzerConfig(holding=holding, time_scale=scale))


async def test_auto_calibration_runs_its_phases_on_the_clock() -> None:
    mock = enabled(1, 3)
    mock.holding[0x5C] = 1  # output hold
    mock.holding[0x73 + 2] = 1  # Ch3 calibrates on range 2
    async with raw_bus(mock) as bus:
        slave = bus.slave(1)
        await slave.write_register(0x07D2, 1)
        calibration = mock.calibration
        assert calibration is not None
        assert calibration.kind == "auto_calibration"
        assert calibration.channels == (1, 3)
        assert calibration.duration_s == pytest.approx(0.4)  # pyright: ignore[reportUnknownMemberType]
        assert [p[0] for p in calibration.phases] == ["zero", "span", "span", "extension"]
        assert calibration.phase_at(0.05) == ("zero", None)
        assert calibration.phase_at(0.25) == ("span", 3)
        assert calibration.phase_at(0.35) == ("extension", None)
        assert calibration.phase_at(0.45) is None
        status = await slave.read_input_registers(0x30, count=1)
        assert status == (1,)
        assert mock.register("range.ch3.current") == (1,)
        assert mock.register("status.ch1.auto_zero_running") == (1,)
        assert mock.register("status.ch3.hold") == (1,)
        await slave.write_register(0x07D3, 1)  # ignored while one runs
        assert running(mock) is calibration
        await anyio.sleep(0.45)
        assert await slave.read_input_registers(0x30, count=1) == (0,)
    assert running(mock) is None
    assert mock.register("range.ch3.current") == (0,)
    assert mock.register("status.ch3.hold") == (0,)
    assert mock.commands == [(0x07D2, 1), (0x07D3, 1)]


async def test_auto_zero_zeroes_and_its_errors_appear_at_the_end() -> None:
    mock = enabled(2)
    mock.calibration_errors = {2: 5}
    async with raw_bus(mock) as bus:
        await bus.slave(1).write_register(0x07D3, 1)
        calibration = mock.calibration
        assert calibration is not None
        assert calibration.kind == "auto_zero"
        assert [p[0] for p in calibration.phases] == ["zero"]
        mock.finish_calibration()
        mock.finish_calibration()  # nothing running: nothing to do
    assert mock.register("error.ch2.e5.active") == (1,)
    assert mock.register("error.ch2.e9.active") == (0,)  # 9 is for auto calibration only
    assert mock.register("status.calibration_error") == (1,)
    assert mock.calibration_errors == {}


async def test_commands_that_change_nothing() -> None:
    mock = enabled(1)
    mock.set_register("display.screen", 8)
    async with raw_bus(mock) as bus:
        slave = bus.slave(1)
        await slave.write_register(0x07D2, 2)  # only 1 means run
        await slave.write_register(0x07D4, 1)  # blowback: no status register to show it
        assert running(mock) is None
        assert mock.register("display.screen") == (8,)
        await slave.write_register(0x07D1, 1)
    assert mock.register("display.screen") == (0,)
    assert mock.commands == [(0x07D2, 2), (0x07D4, 1), (0x07D1, 1)]


async def test_an_ignored_write_is_acknowledged_and_not_stored() -> None:
    mock = analyzer()
    mock.inject(FaultKind.IGNORE)
    async with raw_bus(mock) as bus:
        await bus.slave(1).write_register(0x0049, 1)
    assert mock.holding.get(0x0049, 0) == 0


async def test_an_over_long_write_is_refused_by_the_analyzer() -> None:
    async with raw_bus(analyzer()) as bus:
        with pytest.raises(anymodbus.IllegalDataValueError):
            await bus.slave(1).write_registers(0x0000, [0] * 65)


@pytest.mark.parametrize(
    ("fc", "address", "count", "value"),
    [
        (FC06, 0x07D0, 1, 0x01),  # MODE, into the menus
        (FC06, 0x07D0, 1, 0x02),  # SIDE, the passwords' digits
        (FC06, 0x07D0, 1, 0x60),  # ZERO and ENT at once
        (FC06, 0x07D0, 1, 0x00),  # no key
        (FC10, 0x07D0, 1, 0x40),  # keys are FC06 only
        (FC06, 0x009E, 1, 0x40),  # above the FC06 bound
        (FC10, 0x00A4, 2, 0x00),  # the inferred coefficients
        (FC10, 0x00A0, 6, 0x00),  # runs into them
        (FC10, 0x07D1, 1, 0x00),  # commands are FC06 only
        (FC06, 0x03E8, 1, 0x40),  # factory data
    ],
)
async def test_forbidden_writes_fail_the_test(
    fc: int, address: int, count: int, value: int
) -> None:
    mock = analyzer()

    async def write() -> None:
        async with raw_bus(mock) as bus:
            slave = bus.slave(1)
            if fc == FC06:
                await slave.write_register(address, value)
            else:
                await slave.write_registers(address, [value] * count)

    with pytest.raises(MockWriteViolation, match="outside what fujilib may ever write"):
        await write()
    assert len(mock.violations) == 1
    assert mock.violations[0].key == (fc, address, count)
    assert mock.commands == []
    assert mock.remote_keys == []


# --- Faults ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        (FaultKind.DROP, anymodbus.FrameTimeoutError),
        (FaultKind.CORRUPT_CRC, anymodbus.CRCError),
        (FaultKind.WRONG_FUNCTION, anymodbus.UnexpectedResponseError),
        (FaultKind.GARBAGE, anymodbus.ProtocolError),
        (FaultKind.EXCEPTION, anymodbus.SlaveDeviceFailureError),
    ],
)
async def test_reply_faults(kind: FaultKind, error: type[Exception]) -> None:
    mock = analyzer()
    mock.inject(kind)
    async with raw_bus(mock, timeout=0.05) as bus:
        slave = bus.slave(1)
        with pytest.raises(error):
            await slave.read_input_registers(0, count=3)
        # The fault is used up; the next read is clean.
        await anyio.sleep(0.02)
        assert len(await slave.read_input_registers(0, count=3)) == 3


async def test_an_exception_fault_carries_its_code() -> None:
    mock = analyzer()
    mock.inject(FaultKind.EXCEPTION, exception_code=0x06)
    async with raw_bus(mock) as bus:
        with pytest.raises(anymodbus.SlaveDeviceBusyError):
            await bus.slave(1).read_input_registers(0, count=1)


@pytest.mark.parametrize("count", [3, 1])
async def test_a_wrong_count_reply_is_well_formed_and_refused(count: int) -> None:
    # One word too few (too many for a one-word read), with a valid CRC:
    # anymodbus compares the reply's length with the request (design §4.4).
    mock = analyzer()
    mock.inject(FaultKind.WRONG_COUNT)
    async with raw_bus(mock) as bus:
        with pytest.raises(anymodbus.UnexpectedResponseError, match="register"):
            await bus.slave(1).read_input_registers(0, count=count)
    (exchange,) = mock.exchanges
    assert exchange.reply is not None
    assert verify_crc(exchange.reply)


async def test_a_broadcast_changes_nothing() -> None:
    # The ZP series documents no broadcast; the simulator ignores one.
    mock = analyzer()
    before = dict(mock.holding)
    async with raw_bus(mock) as bus:
        await bus.broadcast_write_register(0x0049, 1)
        await anyio.sleep(0.05)
    assert mock.holding == before
    assert mock.exchanges == []


async def test_a_delayed_reply_arrives_late() -> None:
    mock = analyzer()
    mock.inject(FaultKind.DELAY, delay_s=0.05)
    async with raw_bus(mock, timeout=0.5) as bus:
        start = time.perf_counter()
        await bus.slave(1).read_input_registers(0, count=1)
        assert time.perf_counter() - start >= 0.04


async def test_the_line_records_replies_and_their_times() -> None:
    mock = analyzer()
    mock.inject(FaultKind.DROP, when=lambda r: r.address == 1)
    async with raw_bus(mock, timeout=0.05) as bus:
        slave = bus.slave(1)
        await slave.read_input_registers(0, count=1)
        with pytest.raises(anymodbus.FrameTimeoutError):
            await slave.read_input_registers(1, count=1)
    answered, dropped = mock.exchanges
    assert answered.reply is not None
    assert answered.reply[:2] == b"\x01\x04"
    assert answered.replied_at is not None
    assert answered.replied_at >= answered.request.arrived_at
    assert dropped.reply is None
    assert dropped.replied_at is None


async def test_on_request_sees_each_request_before_it_is_answered() -> None:
    mock = analyzer()
    seen: list[tuple[int, int | None, int | None]] = []

    def change(req: MockRequest) -> None:
        seen.append(req.key)
        mock.set_register("status.ch1.hold", 1)

    mock.on_request = change
    async with raw_bus(mock) as bus:
        words = await bus.slave(1).read_input_registers(0x00A7, count=1)
    assert seen == [(FC04, 0x00A7, 1)]
    assert words == (1,)


async def test_a_failure_inside_the_block_is_raised_as_itself() -> None:
    with pytest.raises(KeyError, match="body"):
        async with mock_transport(analyzer()):
            raise KeyError("body")


async def test_several_failures_stay_grouped() -> None:
    mock = analyzer()

    def explode(request: MockRequest) -> None:
        raise RuntimeError("line")

    mock.on_request = explode
    with pytest.RaisesGroup(RuntimeError, KeyError):
        async with raw_bus(mock) as bus:
            try:
                await bus.slave(1).read_input_registers(0, count=1)
            finally:
                # The line has already failed; fail the block as well.
                raise KeyError("body")


def test_a_corrupted_reply_differs_only_in_its_crc() -> None:
    mock = analyzer()
    req = request(FC04, 0, 1)
    pdu = mock.handle(req)
    clean = mock.reply_frame(req, pdu, None)
    corrupt = mock.reply_frame(req, pdu, mock.inject(FaultKind.CORRUPT_CRC))
    assert clean[:-2] == corrupt[:-2]
    assert clean[-2:] == crc16_modbus_bytes(clean[:-2])
    assert clean[-2:] != corrupt[-2:]


def test_a_wrong_count_fault_leaves_a_write_reply_alone() -> None:
    mock = analyzer()
    req = request(FC06, 0x49, 1, (1,))
    pdu = mock.handle(req)
    assert mock.reply_frame(req, pdu, mock.inject(FaultKind.WRONG_COUNT)) == encode_adu(
        slave_address=1, pdu=pdu
    )
