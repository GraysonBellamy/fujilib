"""The Modbus port and client, against the simulated analyzer (design §4.2-§4.6, §6.4)."""

from __future__ import annotations

import contextlib
import math
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.lowlevel
import anymodbus
import anyserial
import pytest

from fujilib._deadline import Deadline
from fujilib._lock import maybe_acquire
from fujilib.errors import (
    FujiConfigurationError,
    FujiConnectionError,
    FujiModbusIllegalDataAddressError,
    FujiModbusIllegalDataValueError,
    FujiModbusTimeoutError,
    FujiProtocolError,
    FujiResyncRequiredError,
    FujiTimeoutError,
    FujiValidationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.base import ProtocolClient, ProtocolKind
from fujilib.protocol.modbus.client import (
    BlockReply,
    ClientCounters,
    FailureKind,
    PlanReply,
)
from fujilib.protocol.modbus.client import _kind as failure_kind
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.protocol.modbus.read_plan import POLL_PLAN, BlockRead
from fujilib.registry.regions import RegisterTable
from fujilib.testing import (
    DEFAULT_ZPA_BANK,
    FaultKind,
    MockAnalyzer,
    MockAnalyzerConfig,
    fake_transport,
    hex_to_bytes,
    mock_analyzer_pair,
    mock_port,
    mock_transport,
    parse_arrow_fixture,
)
from tests.factories import timing

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from fujilib.protocol.modbus.client import ModbusClient
    from fujilib.testing import MockExchange, MockRequest

pytestmark = pytest.mark.anyio

FC03, FC04, FC06, FC10 = 0x03, 0x04, 0x06, 0x10
MANUAL_FRAMES = Path(__file__).resolve().parents[1] / "fixtures" / "manual_frames.txt"
BANK = DEFAULT_ZPA_BANK.input

#: Timing for tests that are not about timing: no idle, short timeouts, a short window.
FAST: dict[str, Any] = {"inter_frame_idle": 0.0, "request_timeout": 0.05, "resync_window": 0.01}


def fast_pair(
    config: MockAnalyzerConfig | None = None, **overrides: Any
) -> Any:  # an async context manager of (client, mock)
    return mock_analyzer_pair(config, **{**FAST, **overrides})


def words(address: int, count: int) -> tuple[int, ...]:
    return tuple(BANK.get(a, 0) for a in range(address, address + count))


def at(address: int) -> Callable[[MockRequest], bool]:
    def match(request: MockRequest) -> bool:
        return request.address == address

    return match


# --- Golden frames: the manual's bytes on the wire -------------------------------------------


async def test_reads_and_writes_put_the_manuals_bytes_on_the_wire() -> None:
    fc03, fc04, _, fc10 = parse_arrow_fixture(MANUAL_FRAMES)[1:]
    fake = fake_transport(MANUAL_FRAMES)
    async with ModbusPort(fake, startup_settle=0.0, inter_frame_idle=0.0) as port:
        client = port.client(1)
        assert (await client.read(BlockRead(FC03, 0x0004, 2))).words == (0, 1000)
        assert (await client.read(BlockRead(FC04, 0x000C, 3))).words == (1200, 2, 0)
        await client.write_registers(0x0023, [5000, 10, 1000, 10])
    assert fake.writes == [fc03.request, fc04.request, fc10.request]
    assert fake.unmatched == []


async def test_the_manuals_zero_key_frame_is_never_sent() -> None:
    fake = fake_transport(MANUAL_FRAMES)
    async with ModbusPort(fake, startup_settle=0.0) as port:
        with pytest.raises(FujiValidationError, match="write envelope"):
            await port.client(1).write_register(0x07D0, 0x40)
    assert fake.writes == []


async def test_a_write_with_no_reply_has_an_unknown_outcome() -> None:
    # The manual's CRC example has no reply in the fixture.
    fake = fake_transport(MANUAL_FRAMES)
    async with ModbusPort(fake, startup_settle=0.0, request_timeout=0.05) as port:
        client = port.client(1)
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await client.write_register(0x0005, 1000)
    assert fake.writes == [hex_to_bytes("01 06 00 05 03 E8 99 75")]  # sent once, never retried
    assert info.value.context.extra["write_state"] == "unknown"
    assert info.value.context.extra["transmission_started"] is True
    assert client.counters.failures == {FailureKind.TIMEOUT: 1}


# --- Arguments are checked before anything is sent -------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        BlockRead(0x01, 0, 1),
        BlockRead(FC06, 0, 1),
        BlockRead(FC04, 0, 0),
        BlockRead(FC04, 0, 65),
        BlockRead(FC04, 0xFFF0, 32),
        BlockRead(FC04, -1, 2),
    ],
)
async def test_bad_reads_are_refused_before_io(block: BlockRead) -> None:
    async with fast_pair() as (client, mock):
        with pytest.raises(FujiValidationError):
            await client.read(block)
    assert mock.exchanges == []
    assert client.counters.requests == 0


@pytest.mark.parametrize(
    ("address", "values"),
    [
        (0x0010, [0x10000]),
        (0x0010, [-1]),
        (0x0010, [True]),
        (0x0010, [1.5]),
        (0x0010, []),
        (0x0000, [0] * 65),
        (-1, [0]),
        (0xFFFF, [0, 0]),
        (0x00A4, [0, 0]),  # the inferred coefficients, outside the envelope
    ],
)
async def test_bad_writes_are_refused_before_io(address: int, values: list[Any]) -> None:
    async with fast_pair() as (client, mock):
        with pytest.raises(FujiValidationError):
            await client.write_registers(address, values)
    assert mock.exchanges == []


@pytest.mark.parametrize(("address", "value"), [(0x009E, 1), (0x07D0, 0x40), (0x0010, 0x10000)])
async def test_bad_single_writes_are_refused_before_io(address: int, value: int) -> None:
    async with fast_pair() as (client, mock):
        with pytest.raises(FujiValidationError):
            await client.write_register(address, value)
    assert mock.exchanges == []


@pytest.mark.parametrize("station", [0, 32, -1, True, 1.0])
async def test_station_numbers_are_1_to_31(station: Any) -> None:
    async with fast_pair() as (client, _mock):
        with pytest.raises(FujiValidationError, match="station"):
            client.port.client(station)


@pytest.mark.parametrize(
    "options",
    [
        {"request_timeout": 0.0},
        {"request_timeout": 61.0},
        {"request_timeout": math.nan},
        {"inter_frame_idle": -0.001},
        {"startup_settle": math.inf},
        {"resync_window": -1.0},
        {"read_retries": -1},
        {"read_retries": 1.5},
        {"read_retries": True},
    ],
)
async def test_port_settings_are_validated(options: dict[str, Any]) -> None:
    async with mock_transport() as (transport, _line):
        with pytest.raises(FujiValidationError):
            ModbusPort(transport, **options)


# --- Reads, retries and counters -------------------------------------------------------------


async def test_a_read_returns_words_and_timing() -> None:
    async with fast_pair() as (client, mock):
        reply = await client.read(BlockRead(FC04, 0x0000, 9))
    assert reply.words == words(0, 9)
    assert reply.attempts == 1
    assert reply.timing.received_at >= reply.timing.requested_at
    assert reply.timing.t_reply_mono_ns >= reply.timing.t_request_mono_ns
    assert reply.raw == b"".join(w.to_bytes(2, "big") for w in reply.words)
    assert reply.to_bank() == dict(zip(range(9), reply.words, strict=True))
    assert mock.transactions() == [(FC04, 0x0000, 9)]
    assert client.counters == ClientCounters(requests=1)


@pytest.mark.parametrize(
    ("fault", "kind"),
    [
        (FaultKind.DROP, FailureKind.TIMEOUT),
        (FaultKind.CORRUPT_CRC, FailureKind.FRAME),
        (FaultKind.WRONG_COUNT, FailureKind.UNEXPECTED),
        (FaultKind.WRONG_FUNCTION, FailureKind.UNEXPECTED),
        (FaultKind.GARBAGE, FailureKind.FRAME),
    ],
)
async def test_a_damaged_reply_is_retried_and_counted(fault: FaultKind, kind: FailureKind) -> None:
    async with fast_pair() as (client, mock):
        mock.inject(fault)
        reply = await client.read(BlockRead(FC04, 0x0000, 3))
    assert reply.words == words(0, 3)
    assert reply.attempts == 2
    assert mock.transactions() == [(FC04, 0, 3)] * 2
    assert client.counters == ClientCounters(requests=2, retries=1, recovered=1, failures={kind: 1})
    assert client.recoverable_error_count == 1


async def test_retries_run_out() -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.DROP, times=None)
        with pytest.raises(FujiModbusTimeoutError) as info:
            await client.read(BlockRead(FC04, 0x0000, 3))
    assert len(mock.exchanges) == 3
    assert client.counters.retries == 2
    assert client.counters.recovered == 0
    assert client.counters.failures == {FailureKind.TIMEOUT: 3}
    context = info.value.context
    assert (context.port, context.address, context.function_code) == ("mock://zp", 1, FC04)
    assert context.register_address == 0
    assert context.extra["attempt"] == 3
    assert context.extra["count"] == 3
    assert context.elapsed_s is not None


async def test_a_persistently_short_reply_does_not_answer_the_request() -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.WRONG_COUNT, times=None)
        with pytest.raises(FujiProtocolError) as info:
            await client.read(BlockRead(FC04, 0x0000, 3))
    assert type(info.value) is FujiProtocolError
    assert isinstance(info.value.__cause__, anymodbus.UnexpectedResponseError)
    assert client.counters.failures == {FailureKind.UNEXPECTED: 3}


async def test_unexpected_replies_are_protocol_errors_after_the_retries() -> None:
    async with fast_pair(read_retries=0) as (client, mock):
        mock.inject(FaultKind.WRONG_FUNCTION)
        with pytest.raises(FujiProtocolError) as info:
            await client.read(BlockRead(FC04, 0x0000, 3))
    assert type(info.value) is FujiProtocolError
    assert len(mock.exchanges) == 1


async def test_an_exception_reply_is_an_answer_and_is_not_retried() -> None:
    async with fast_pair() as (client, mock):
        with pytest.raises(FujiModbusIllegalDataAddressError) as info:
            await client.read(BlockRead(FC04, 0x00C2, 1))
    assert len(mock.exchanges) == 1
    assert info.value.context.extra["exception_code"] == 2
    assert client.counters.failures == {FailureKind.EXCEPTION: 1}
    assert client.counters.retries == 0


async def test_a_plan_is_read_in_order_and_merged_per_table() -> None:
    plan = (BlockRead(FC03, 0x0000, 4), BlockRead(FC04, 0x0000, 3), BlockRead(FC04, 0x0010, 2))
    async with fast_pair() as (client, mock):
        reply = await client.read_plan(plan)
    assert mock.transactions() == [b.key for b in plan]
    assert isinstance(reply, PlanReply)
    assert dict(reply.holding) == {a: DEFAULT_ZPA_BANK.holding[a] for a in range(4)}
    assert dict(reply.input) == {a: BANK.get(a, 0) for a in (0, 1, 2, 0x10, 0x11)}
    assert reply.bank(RegisterTable.INPUT) == reply.input
    assert len(reply.timings) == 3
    assert reply.raw == b"".join(r.raw for r in reply.replies)


async def test_a_failed_second_block_keeps_the_first_blocks_words() -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.DROP, times=None, when=at(0x0083))
        with pytest.raises(FujiModbusTimeoutError) as info:
            await client.read_plan(POLL_PLAN, command="poll")
    ((key, block_words),) = info.value.context.extra["completed"]
    assert key == (FC04, 0x0000, 61)
    assert block_words == words(0, 61)
    assert info.value.context.command_name == "poll"
    assert isinstance(info.value.__cause__, Exception)


async def test_a_failed_first_block_has_nothing_completed() -> None:
    async with fast_pair(read_retries=0) as (client, mock):
        mock.inject(FaultKind.DROP)
        with pytest.raises(FujiModbusTimeoutError) as info:
            await client.read_plan(POLL_PLAN)
    assert "completed" not in info.value.context.extra
    assert len(mock.exchanges) == 1


# --- Writes -------------------------------------------------------------------------------------


async def test_writes_reach_the_analyzer() -> None:
    async with fast_pair() as (client, mock):
        single = await client.write_register(0x0049, 1)
        await client.write_registers(0x0023, [5000, 10, 1000, 10])
        await client.write_register(0x07D1, 1)
    assert mock.holding[0x0049] == 1
    assert [mock.holding[a] for a in range(0x23, 0x27)] == [5000, 10, 1000, 10]
    assert mock.commands == [(0x07D1, 1)]
    assert mock.transactions() == [(FC06, 0x0049, 1), (FC10, 0x0023, 4), (FC06, 0x07D1, 1)]
    assert single.latency_s >= 0


@pytest.mark.parametrize("fault", [FaultKind.DROP, FaultKind.CORRUPT_CRC, FaultKind.GARBAGE])
async def test_a_write_is_never_retried(fault: FaultKind) -> None:
    async with fast_pair() as (client, mock):
        mock.inject(fault)
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await client.write_register(0x0049, 1)
    assert len(mock.exchanges) == 1
    assert mock.holding[0x0049] == 1  # the analyzer applied it; only the reply was lost
    assert client.counters.retries == 0
    assert info.value.__cause__ is not None


async def test_a_refused_write_is_a_definite_answer() -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.EXCEPTION, exception_code=0x03)
        with pytest.raises(FujiModbusIllegalDataValueError):
            await client.write_registers(0x0023, [1, 2])
    assert len(mock.exchanges) == 1


async def test_a_write_whose_deadline_expires_in_flight_has_an_unknown_outcome() -> None:
    async with fast_pair(request_timeout=0.5) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.2)
        deadline = Deadline.after(0.05, operation="set_hold")
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await client.write_register(0x005C, 1, deadline=deadline)
    assert isinstance(info.value.__cause__, FujiTimeoutError)
    assert client.counters.failures == {FailureKind.CANCELLED: 1}


async def test_a_write_whose_deadline_expires_in_the_queue_was_never_sent() -> None:
    async with fast_pair() as (client, mock):
        async with anyio.create_task_group() as tg:

            async def hold(started: anyio.Event) -> None:
                async with maybe_acquire(client.port.lock):
                    started.set()
                    await anyio.sleep(0.1)

            started = anyio.Event()
            _ = tg.start_soon(hold, started)
            await started.wait()
            with pytest.raises(FujiTimeoutError) as info:
                await client.write_register(0x005C, 1, deadline=Deadline.after(0.02, operation="w"))
            assert type(info.value) is FujiTimeoutError
    assert mock.exchanges == []


async def test_a_write_on_a_closed_transport_is_a_connection_error() -> None:
    async with fast_pair() as (client, _mock):
        await client.port.transport.aclose()
        with pytest.raises(FujiConnectionError):
            await client.write_register(0x0049, 1)


# --- Deadlines -----------------------------------------------------------------------------------


async def test_a_read_deadline_covers_the_retries() -> None:
    async with fast_pair(request_timeout=0.05, read_retries=10) as (client, mock):
        mock.inject(FaultKind.DROP, times=None)
        with pytest.raises(FujiTimeoutError) as info:
            await client.read(
                BlockRead(FC04, 0, 1), deadline=Deadline.after(0.12, operation="poll")
            )
    assert type(info.value) is FujiTimeoutError
    assert info.value.context.command_name == "poll"
    assert len(mock.exchanges) < 11
    assert client.counters.failures[FailureKind.CANCELLED] == 1


async def test_a_read_deadline_covers_queue_time() -> None:
    async with fast_pair() as (client, mock):
        started = anyio.Event()

        async def hold() -> None:
            async with maybe_acquire(client.port.lock):
                started.set()
                await anyio.sleep(0.1)

        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(hold)
            await started.wait()
            with pytest.raises(FujiTimeoutError):
                await client.read(
                    BlockRead(FC04, 0, 1), deadline=Deadline.after(0.02, operation="p")
                )
    assert mock.exchanges == []


async def test_a_held_lock_is_reused_by_its_holder() -> None:
    async with fast_pair() as (client, mock):
        async with maybe_acquire(client.port.lock):
            await client.read(BlockRead(FC04, 0, 1))
            await client.read(BlockRead(FC04, 1, 1))
    assert len(mock.exchanges) == 2


# --- Uncertain outcomes (design §6.4) ------------------------------------------------------------


async def test_a_write_whose_port_fails_after_sending_has_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fast_pair() as (client, mock):
        fail_drain(monkeypatch, client)
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await client.write_register(0x07D1, 1)
        await anyio.sleep(0.05)  # let the simulator take in the request
    assert info.value.context.extra["failure"] == "connection"
    assert isinstance(info.value.__cause__, anymodbus.TransportError)
    assert mock.commands == [(0x07D1, 1)]  # it was applied


@pytest.mark.parametrize(
    ("function_code", "failure"),
    # 06 answered as 10; and 07, a code a client never sends, with a valid CRC.
    [(None, "unexpected"), (0x07, "unexpected")],
)
async def test_a_write_answered_with_another_function_code_has_an_unknown_outcome(
    function_code: int | None, failure: str
) -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.WRONG_FUNCTION, function_code=function_code)
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await client.write_register(0x0049, 7)
    assert info.value.context.extra["failure"] == failure
    assert mock.holding[0x0049] == 7


async def test_a_damaged_function_code_on_a_read_is_retried() -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.WRONG_FUNCTION, function_code=0x07)
        reply = await client.read(A)
    assert reply.words == words(A.address, 3)
    # anymodbus reads a reply with a code it never sends to the idle gap and lets
    # the CRC decide: this one is intact, so it does not answer the request.
    assert client.counters.failures == {FailureKind.UNEXPECTED: 1}
    assert client.counters.recovered == 1


async def test_a_refused_write_changes_nothing() -> None:
    async with fast_pair() as (client, mock):
        mock.inject(FaultKind.EXCEPTION, times=2, exception_code=0x03)
        before = mock.holding.get(0x0049, 0)
        with pytest.raises(FujiModbusIllegalDataValueError):
            await client.write_register(0x0049, before + 1)
        with pytest.raises(FujiModbusIllegalDataValueError):
            await client.write_register(0x07D2, 1)
    assert mock.holding.get(0x0049, 0) == before
    assert mock.commands == []


async def test_a_shared_deadline_enforced_outside_still_reports_the_write() -> None:
    # A caller holding the same deadline around the call catches its expiry
    # first; the write's unknown outcome must not turn into a bare timeout.
    async with fast_pair(request_timeout=0.5) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.2)
        dl = Deadline.after(0.05, operation="set_hold")
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            with dl.enforce():
                await client.write_register(0x005C, 1, deadline=dl)
        assert info.value.context.extra["failure"] == "cancelled"
        await anyio.sleep(0.25)  # the task is not left cancelled
        assert mock.holding[0x005C] == 1


async def test_a_shared_deadline_enforced_outside_keeps_the_blocks_read() -> None:
    async with fast_pair(request_timeout=0.5) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.3, when=at(0x0083))
        dl = Deadline.after(0.1, operation="poll")
        with pytest.raises(FujiTimeoutError) as info:
            with dl.enforce():
                await client.read_plan(POLL_PLAN, deadline=dl, command="poll")
        await anyio.lowlevel.checkpoint()  # the task is not left cancelled
    ((key, _words),) = info.value.context.extra["completed"]
    assert key == (FC04, 0x0000, 61)
    assert (info.value.context.port, info.value.context.address) == ("mock://zp", 1)


async def test_a_deadline_that_expires_mid_poll_keeps_the_blocks_read() -> None:
    async with fast_pair(request_timeout=0.5) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.3, when=at(0x0083))
        with pytest.raises(FujiTimeoutError) as info:
            await client.read_plan(POLL_PLAN, deadline=Deadline.after(0.1, operation="poll"))
    assert type(info.value) is FujiTimeoutError
    context = info.value.context
    assert context.extra["completed"][0][0] == (FC04, 0x0000, 61)
    assert (context.port, context.address, context.command_name) == ("mock://zp", 1, "poll")


async def test_an_expired_deadline_sends_nothing_even_under_a_held_lock() -> None:
    async with fast_pair() as (client, mock):
        await client.read(A)  # past the startup, so ready() does not sleep
        async with maybe_acquire(client.port.lock):
            with pytest.raises(FujiTimeoutError) as info:
                await client.write_register(0x005C, 1, deadline=Deadline.after(0, operation="w"))
    assert type(info.value) is FujiTimeoutError
    assert len(mock.exchanges) == 1
    assert client.port.quiet_remaining() == 0


# --- Late replies and the quiet window (design §4.2) --------------------------------------------


# The reply to the first read (A) arrives 150 ms after its request, after the
# 100 ms timeout, while a second read of the same length (B) is outstanding.
LATE: dict[str, Any] = {"inter_frame_idle": 0.0, "request_timeout": 0.1, "read_retries": 0}
A, B = BlockRead(FC04, 0x0000, 3), BlockRead(FC04, 0x0010, 3)


async def test_without_a_quiet_window_a_late_reply_answers_the_wrong_read() -> None:
    # The hazard the window exists for: FC04 replies carry no address.
    async with mock_analyzer_pair(**LATE, resync_window=0.0) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.15, when=at(A.address))
        with pytest.raises(FujiModbusTimeoutError):
            await client.read(A)
        stale = await client.read(B)
    assert stale.words == words(A.address, 3) != words(B.address, 3)


async def test_after_a_timeout_the_quiet_window_lets_the_late_reply_land() -> None:
    async with mock_analyzer_pair(**LATE, resync_window=0.1) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.15, when=at(A.address))
        with pytest.raises(FujiModbusTimeoutError):
            await client.read(A)
        assert client.port.quiet_remaining() > 0
        assert (await client.read(B)).words == words(B.address, 3)


async def test_after_a_cancellation_the_quiet_window_lets_the_late_reply_land() -> None:
    async with mock_analyzer_pair(**LATE, resync_window=0.1) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.06, when=at(A.address))
        with anyio.move_on_after(0.02):
            await client.read(A)
        assert client.counters.failures == {FailureKind.CANCELLED: 1}
        assert (await client.read(B)).words == words(B.address, 3)


@pytest.mark.parametrize("fault", [FaultKind.WRONG_COUNT, FaultKind.WRONG_FUNCTION])
async def test_mismatched_replies_start_a_quiet_window(fault: FaultKind) -> None:
    async with fast_pair(read_retries=0, resync_window=1.0) as (client, mock):
        mock.inject(fault)
        with pytest.raises(FujiProtocolError):
            await client.read(A)
        assert client.port.quiet_remaining() > 0.5


async def test_certain_outcomes_leave_no_quiet_window() -> None:
    async with fast_pair(resync_window=1.0) as (client, _mock):
        await client.read(A)
        with pytest.raises(FujiModbusIllegalDataAddressError):
            await client.read(BlockRead(FC04, 0x00C2, 1))
        assert client.port.quiet_remaining() == 0


async def test_a_deadline_inside_the_quiet_window_is_refused_without_io() -> None:
    async with fast_pair(read_retries=0, resync_window=1.0) as (client, mock):
        mock.inject(FaultKind.DROP)
        with pytest.raises(FujiModbusTimeoutError):
            await client.read(A)
        with pytest.raises(FujiResyncRequiredError):
            await client.read(B, deadline=Deadline.after(0.05, operation="poll"))
    assert len(mock.exchanges) == 1


# --- The inter-frame gap, measured at the analyzer ----------------------------------------------

IDLE = 0.02
GAPPED: dict[str, Any] = {
    "inter_frame_idle": IDLE,
    "request_timeout": 0.05,
    "resync_window": 0.0,
    "read_retries": 0,
}


def gapped(**overrides: Any) -> Any:  # an async context manager of (client, mock)
    return mock_analyzer_pair(**{**GAPPED, **overrides})


def gap_after(first: MockExchange, second: MockRequest) -> float:
    end = first.replied_at if first.replied_at is not None else first.request.arrived_at
    return second.arrived_at - end


async def after(client: ModbusClient, block: BlockRead) -> None:
    with contextlib.suppress(FujiProtocolError):
        await client.read(block)


@pytest.mark.parametrize(
    ("fault", "extra"),
    [
        (None, 0.0),
        (FaultKind.EXCEPTION, 0.0),
        (FaultKind.CORRUPT_CRC, 0.0),
        (FaultKind.DROP, 0.05),  # the request timeout passes before the transaction ends
    ],
)
async def test_the_gap_runs_from_the_end_of_every_transaction(
    fault: FaultKind | None, extra: float
) -> None:
    async with gapped() as (client, mock):
        if fault is not None:
            mock.inject(fault)
        await after(client, A)
        await client.read(B)
    first, second = mock.exchanges
    assert gap_after(first, second.request) >= IDLE + extra


async def test_the_gap_holds_after_a_cancelled_read() -> None:
    async with gapped(request_timeout=0.5) as (client, mock):
        mock.inject(FaultKind.DROP)
        with anyio.move_on_after(0.03):
            await client.read(A)
        await client.read(B)
    first, second = mock.exchanges
    assert second.request.arrived_at - first.request.arrived_at >= 0.03 + IDLE


async def test_the_gap_holds_between_the_clients_own_retries() -> None:
    async with gapped(read_retries=1) as (client, mock):
        mock.inject(FaultKind.DROP)
        await client.read(A)
    first, second = mock.exchanges
    assert second.request.arrived_at - first.request.arrived_at >= 0.05 + IDLE


async def test_a_request_is_timed_after_the_gap_not_before() -> None:
    idle = 0.05
    async with gapped(inter_frame_idle=idle) as (client, _mock):
        first = await client.read(A)
        second = await client.read(B)
    waited_ns = second.timing.t_request_mono_ns - first.timing.t_reply_mono_ns
    assert waited_ns >= (idle - 0.001) * 1e9
    # The request goes out straight after the stamp, so the round trip does not
    # include the gap.
    assert second.timing.latency_s < idle


async def test_an_interrupted_startup_settle_is_not_repeated() -> None:
    async with fast_pair(startup_settle=0.5) as (client, mock):
        with anyio.move_on_after(0.02):
            await client.read(A)
        assert mock.exchanges == []  # cancelled while settling, before any request
        interrupted = anyio.current_time()
        await client.read(A)
    assert mock.exchanges[0].request.arrived_at - interrupted < 0.25


async def test_the_startup_settle_is_waited_once() -> None:
    async with fast_pair(startup_settle=0.05) as (client, mock):
        opened = anyio.current_time()
        await client.read(A)
        await client.read(B)
    first, second = mock.exchanges
    assert first.request.arrived_at - opened >= 0.05
    assert second.request.arrived_at - first.request.arrived_at < 0.05


# --- Port lifecycle and ownership ----------------------------------------------------------------


async def test_a_transport_carries_one_open_port() -> None:
    async with mock_transport(MockAnalyzer()) as (transport, _line):
        first = ModbusPort(transport)
        with pytest.raises(FujiConfigurationError, match="already has an open Modbus port"):
            ModbusPort(transport)
        await first.aclose()
        second = ModbusPort(transport)
        await second.aclose()
        await first.aclose()  # idempotent
        assert transport.is_open  # neither port owned it


async def test_closing_waits_for_the_transaction_in_flight() -> None:
    async with fast_pair(request_timeout=0.5) as (client, mock):
        mock.inject(FaultKind.DELAY, delay_s=0.1)
        port = client.port
        results: list[tuple[int, ...]] = []

        async def read() -> None:
            results.append((await client.read(A)).words)

        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(read)
            await anyio.sleep(0.02)  # the read is waiting for its delayed reply
            await port.aclose()
            assert results == [words(A.address, 3)]  # closing waited for it
        # The transport is free again, and nothing of the old port is in flight.
        async with ModbusPort(port.transport, **FAST) as again:
            assert (await again.client(1).read(B)).words == words(B.address, 3)


async def test_two_tasks_closing_at_once_close_once() -> None:
    async with mock_transport(analyzer := MockAnalyzer(DEFAULT_ZPA_BANK)) as (transport, _):
        port = ModbusPort(transport, owns_transport=True, **FAST)
        analyzer.inject(FaultKind.DELAY, delay_s=0.1)
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(port.client(1).read, A)
            await anyio.sleep(0.02)
            _ = tg.start_soon(port.aclose)
            _ = tg.start_soon(port.aclose)
        assert port.closed
        assert not transport.is_open


async def test_an_owning_port_closes_its_transport() -> None:
    async with mock_transport(MockAnalyzer()) as (transport, _line):
        async with ModbusPort(transport, owns_transport=True) as port:
            assert "open" in repr(port)
        assert not transport.is_open
        assert port.closed
        assert "closed" in repr(port)


async def test_a_closed_port_refuses_requests() -> None:
    async with fast_pair() as (client, mock):
        await client.port.aclose()
        with pytest.raises(FujiConnectionError, match="closed"):
            await client.read(A)
    assert mock.exchanges == []


async def test_a_closed_transport_is_refused_before_io() -> None:
    async with fast_pair() as (client, mock):
        await client.port.transport.aclose()
        with pytest.raises(FujiConnectionError, match="transport"):
            await client.read(A)
    assert client.counters == ClientCounters()
    assert mock.exchanges == []


async def test_a_port_that_fails_mid_read_is_a_connection_error_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fast_pair() as (client, mock):
        fail_drain(monkeypatch, client)
        with pytest.raises(FujiConnectionError) as info:
            await client.read(A)
        await anyio.sleep(0.05)  # let the simulator take in the request
    # anymodbus reports a failing port as a TransportError (an OSError).
    assert isinstance(info.value.__cause__, anymodbus.TransportError)
    assert client.counters.failures == {FailureKind.CONNECTION: 1}
    assert client.counters.retries == 0
    assert len(mock.exchanges) == 1  # the request did go out


async def test_port_and_client_properties() -> None:
    async with fast_pair(read_retries=1) as (client, _mock):
        port = client.port
        assert port.client(1) is client
        assert port.client(2) is not client
        assert (port.label, port.protocol) == ("mock://zp", ProtocolKind.MODBUS_RTU)
        assert (port.request_timeout, port.inter_frame_idle) == (0.05, 0.0)
        assert (port.read_retries, port.resync_window) == (1, 0.01)
        assert port.transport.label == "mock://zp"
        assert (client.address, client.label) == (1, "mock://zp")
        assert repr(client) == "<ModbusClient mock://zp station 1>"
        protocol_client: ProtocolClient = client
        assert protocol_client.recoverable_error_count == 0


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (anymodbus.ConfigurationError("bad address"), FailureKind.OTHER),
        (ValueError("count"), FailureKind.OTHER),
        (anymodbus.ModbusError("other"), FailureKind.OTHER),
        (anymodbus.FrameTimeoutError("silent"), FailureKind.TIMEOUT),
        (anymodbus.ModbusUnsupportedFunctionError("fc 0x07"), FailureKind.OTHER),
        (anymodbus.UnexpectedResponseError("fc 3, expected 4"), FailureKind.UNEXPECTED),
        (anymodbus.CRCError("crc"), FailureKind.FRAME),
        (
            anymodbus.IllegalDataAddressError(function_code=4, exception_code=2),
            FailureKind.EXCEPTION,
        ),
        (anymodbus.BusClosedError("closed"), FailureKind.CONNECTION),
        (OSError("gone"), FailureKind.CONNECTION),
        (anyio.BusyResourceError("receiving"), FailureKind.CONNECTION),
    ],
)
def test_failure_kinds(exc: BaseException, kind: FailureKind) -> None:
    assert failure_kind(exc) is kind


async def test_a_failure_raised_before_any_attempt_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # anymodbus can refuse a call outright (a closed bus, an argument it
    # rejects); no attempt is reported, and the client still counts it.
    async with fast_pair() as (client, mock):

        async def refuse(self: object, address: int, *, count: int) -> tuple[int, ...]:
            raise anymodbus.ConfigurationError("refused")

        monkeypatch.setattr(anymodbus.Slave, "read_input_registers", refuse)
        with pytest.raises(FujiConfigurationError, match="refused"):
            await client.read(A)
    assert client.counters == ClientCounters(failures={FailureKind.OTHER: 1})
    assert mock.exchanges == []


def fail_drain(monkeypatch: pytest.MonkeyPatch, client: ModbusClient) -> None:
    """Make the host port fail after sending, while anymodbus waits for it to drain."""

    async def drain(self: object) -> None:
        raise anyserial.SerialError("device reports an I/O error")

    monkeypatch.setattr(type(client.port.transport.stream), "drain", drain)


def test_counters_count_failures_by_kind() -> None:
    counters = ClientCounters()
    counters.count_failure(FailureKind.TIMEOUT)
    counters.count_failure(FailureKind.TIMEOUT)
    counters.count_failure(FailureKind.FRAME)
    assert counters.failures == {FailureKind.TIMEOUT: 2, FailureKind.FRAME: 1}


def test_block_reply_shapes() -> None:
    reply = BlockReply(BlockRead(FC04, 0x10, 2), (1, 0xABCD), timing(), attempts=1)
    assert reply.raw == b"\x00\x01\xab\xcd"
    assert reply.to_bank() == {0x10: 1, 0x11: 0xABCD}
    plan = PlanReply((reply,))
    assert plan.holding == {}
    assert plan.input == {0x10: 1, 0x11: 0xABCD}


# --- Several stations on one line ---------------------------------------------------------------


@pytest.fixture
def stations() -> tuple[MockAnalyzer, MockAnalyzer]:
    first = MockAnalyzer(replace(DEFAULT_ZPA_BANK, station=1))
    second = MockAnalyzer(MockAnalyzerConfig(station=2, input={0: 222, 1: 2, 2: 0}))
    return first, second


async def test_stations_share_one_line(stations: tuple[MockAnalyzer, MockAnalyzer]) -> None:
    first, second = stations
    async with mock_port(first, second, **FAST) as (port, line):
        one, two = port.client(1), port.client(2)
        assert (await two.read(A)).words == (222, 2, 0)
        assert (await one.read(A)).words == words(0, 3)
    assert [e.request.station for e in line.exchanges] == [2, 1]
    assert first.transactions() == second.transactions() == [A.key]


async def test_an_absent_station_does_not_stall_the_others(
    stations: tuple[MockAnalyzer, MockAnalyzer],
) -> None:
    first, second = stations
    finished: dict[int, float] = {}
    errors: list[Exception] = []

    async with mock_port(first, second, **FAST) as (port, line):
        start = anyio.current_time()

        async def poll(address: int) -> None:
            try:
                await port.client(address).read_plan(POLL_PLAN)
            except FujiModbusTimeoutError as exc:
                errors.append(exc)
            finished[address] = anyio.current_time() - start

        async with anyio.create_task_group() as tg:
            for address in (3, 1, 2):
                _ = tg.start_soon(poll, address)
    assert set(finished) == {1, 2, 3}
    assert len(errors) == 1
    assert errors[0].context.address == 3  # type: ignore[attr-defined]
    # Station 3 costs at most one read's retry budget (3 x 50 ms) plus the window.
    assert max(finished[1], finished[2]) < 1.0
    polls = [e.request.station for e in line.exchanges]
    assert polls.count(3) == 3
    # The operation lock keeps each poll's two blocks together.
    for station in (1, 2):
        index = polls.index(station)
        assert polls[index : index + 2] == [station, station]


@pytest.fixture
async def pair() -> AsyncGenerator[tuple[ModbusClient, MockAnalyzer]]:
    async with fast_pair() as (client, mock):
        yield client, mock


async def test_the_pair_fixture_reads(pair: tuple[ModbusClient, MockAnalyzer]) -> None:
    client, mock = pair
    await client.read(A)
    assert mock.transactions() == [A.key]
