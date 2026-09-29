"""The recorder (design §7.6): schedule, error samples, overflow, disconnects.

Most tests run the recorder on a manual clock: a poll "takes" whatever time
the stub source adds to it, and sleeping jumps the clock to the deadline, so
tick, late and drift counts are exact on every backend and platform. The last
tests run :func:`record` itself, on the event loop's clock, over the
simulated analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

import anyio
import pytest

from fujilib import (
    AcquisitionSummary,
    DeviceResult,
    FujiConfigurationError,
    FujiConnectionError,
    FujiError,
    FujiModbusTimeoutError,
    FujiValidationError,
    OverflowPolicy,
    PollSourceAdapter,
    ProtocolKind,
    ReconnectPolicy,
    Recording,
    SourceLayout,
    record,
    sample_to_row,
)
from fujilib.registry.channels import ChannelId, Gas
from fujilib.streaming.recorder import Batch, _record
from fujilib.testing import FaultKind
from tests.facade import FC04, POLL, analyzer_on, bench, replugging, when
from tests.factories import frame, reading

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from fujilib.devices.models import Frame

pytestmark = pytest.mark.anyio

CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)


class ManualClock:
    """A clock that moves only when told to: by a poll's cost, or by sleeping."""

    def __init__(self) -> None:
        self.t = 100.0

    def now(self) -> float:
        return self.t

    async def sleep_until(self, deadline: float) -> None:
        self.t = max(self.t, deadline)
        await anyio.lowlevel.checkpoint()

    def advance(self, seconds: float) -> None:
        self.t += seconds


type Outcome = Frame | FujiError | None
"""What one analyzer's poll gives: a frame, an error, or no entry at all."""


@dataclass
class StubSource:
    """A poll source on a :class:`ManualClock`.

    ``script(poll_number, name)`` decides each outcome; ``cost(poll_number)``
    is how long each poll takes.
    """

    clock: ManualClock
    names: tuple[str, ...] = ("zpa",)
    channels: tuple[ChannelId, ...] = CHANNELS
    reopenable: bool = False
    script: Callable[[int, str], Outcome] = lambda n, name: frame()
    cost: Callable[[int], float] = lambda n: 0.0
    reconnects: list[str] = field(default_factory=list[str])
    reconnect_errors: list[FujiError] = field(default_factory=list[FujiError])
    on_reconnect: Callable[[], None] = lambda: None
    polls: int = 0
    starts: list[float] = field(default_factory=list[float])

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        self.polls += 1
        self.starts.append(self.clock.now())
        self.clock.advance(self.cost(self.polls))
        await anyio.lowlevel.checkpoint()
        out: dict[str, DeviceResult[Frame]] = {}
        for name in names or self.names:
            outcome = self.script(self.polls, name)
            if isinstance(outcome, FujiError):
                out[name] = DeviceResult.failure(outcome)
            elif outcome is not None:
                out[name] = DeviceResult.success(outcome)
        return MappingProxyType(out)

    def layout(self, names: Sequence[str] | None = None) -> Mapping[str, SourceLayout]:
        chosen = [n for n in self.names if names is None or n in names]
        return {
            n: SourceLayout(
                address=1 + i,
                protocol=ProtocolKind.MODBUS_RTU,
                channels=self.channels,
                reopenable=self.reopenable,
            )
            for i, n in enumerate(chosen)
        }

    async def reconnect(self, name: str) -> None:
        self.reconnects.append(name)
        await anyio.lowlevel.checkpoint()
        if self.reconnect_errors:
            raise self.reconnect_errors.pop(0)
        self.on_reconnect()


def recording(
    source: StubSource,
    *,
    rate_hz: float = 10.0,
    duration: float | None = 1.0,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    reconnect: ReconnectPolicy | None = None,
) -> AbstractAsyncContextManager[Recording[Batch]]:
    return _record(
        source,
        rate_hz=rate_hz,
        duration=duration,
        names=names,
        overflow=overflow,
        buffer_size=buffer_size,
        reconnect=reconnect,
        clock=source.clock,
    )


async def collect(rec: Recording[Batch]) -> list[Batch]:
    return [batch async for batch in rec]


# --- Schedule --------------------------------------------------------------------------------


async def test_a_recording_makes_its_ticks_on_schedule() -> None:
    source = StubSource(ManualClock(), cost=lambda n: 0.02)
    async with recording(source, rate_hz=10.0, duration=1.0) as rec:
        batches = await collect(rec)
        assert rec.rate_hz == 10.0
    assert len(batches) == 10
    assert [round(t - 100.0, 6) for t in source.starts] == [round(k * 0.1, 6) for k in range(10)]
    summary = rec.summary
    assert (summary.samples_emitted, summary.samples_late, summary.samples_dropped) == (10, 0, 0)
    assert summary.target_total_samples == 10
    assert summary.max_drift_ms == 0.0
    assert summary.finished_at is not None
    sample = batches[0]["zpa"]
    assert sample.frame is not None
    assert (sample.device, sample.address, sample.channels) == ("zpa", 1, CHANNELS)


@pytest.mark.parametrize(
    ("rate_hz", "duration", "ticks"),
    [(1.0, 2.5, 3), (10.0, 0.3, 3), (10.0, 1.0, 10), (20.0, 0.05, 1), (2.0, 0.6, 2)],
)
async def test_every_tick_due_within_the_duration(
    rate_hz: float, duration: float, ticks: int
) -> None:
    source = StubSource(ManualClock())
    async with recording(source, rate_hz=rate_hz, duration=duration) as rec:
        assert len(await collect(rec)) == ticks
    assert rec.summary.target_total_samples == ticks


async def test_a_short_duration_still_makes_one_tick() -> None:
    source = StubSource(ManualClock())
    async with recording(source, rate_hz=1.0, duration=0.1) as rec:
        assert len(await collect(rec)) == 1


async def test_an_overrun_skips_whole_slots_and_counts_them_late() -> None:
    # Poll 3 starts at 0.2 s and takes 0.35 s at 10 Hz: slots 3 and 4 pass
    # entirely, and the next poll is 0.05 s late in slot 5.
    source = StubSource(ManualClock(), cost=lambda n: 0.35 if n == 3 else 0.0)
    async with recording(source, rate_hz=10.0, duration=1.0) as rec:
        batches = await collect(rec)
    summary = rec.summary
    assert summary.samples_late == 2
    assert len(batches) == summary.samples_emitted == 8
    assert summary.samples_emitted + summary.samples_late == summary.target_total_samples
    assert round(source.starts[3] - 100.0, 6) == 0.55
    assert summary.max_drift_ms == pytest.approx(50.0)  # pyright: ignore[reportUnknownMemberType]


async def test_drift_is_how_late_a_poll_starts() -> None:
    source = StubSource(ManualClock(), cost=lambda n: 0.15 if n == 1 else 0.0)
    async with recording(source, rate_hz=10.0, duration=0.5) as rec:
        _ = await collect(rec)
    assert rec.summary.samples_late == 0
    assert rec.summary.max_drift_ms == pytest.approx(50.0)  # pyright: ignore[reportUnknownMemberType]


async def test_an_overrun_without_a_duration() -> None:
    # Poll 2 starts at 0.1 s and takes 0.25 s: slot 2 passes; slot 3 starts 0.05 s late.
    source = StubSource(ManualClock(), cost=lambda n: 0.25 if n == 2 else 0.0)
    async with recording(source, duration=None) as rec:
        batches: list[Batch] = []
        async for batch in rec:
            batches.append(batch)
            if len(batches) == 4:
                break
    assert rec.summary.samples_late == 1


async def test_late_ticks_past_the_end_are_not_counted() -> None:
    source = StubSource(ManualClock(), cost=lambda n: 5.0 if n == 2 else 0.0)
    async with recording(source, rate_hz=10.0, duration=0.5) as rec:
        batches = await collect(rec)
    assert len(batches) == 2
    assert rec.summary.samples_late == 3
    assert rec.summary.samples_emitted + rec.summary.samples_late == 5


async def test_without_a_duration_it_runs_until_the_block_exits() -> None:
    source = StubSource(ManualClock())
    async with recording(source, duration=None) as rec:
        batches: list[Batch] = []
        async for batch in rec:
            batches.append(batch)
            if len(batches) == 25:
                break
        assert rec.summary.target_total_samples is None
    assert len(batches) == 25
    assert rec.summary.finished_at is not None


# --- Failed polls ----------------------------------------------------------------------------


async def test_a_failed_poll_is_an_error_sample_with_the_same_row() -> None:
    error = FujiModbusTimeoutError("no reply")
    source = StubSource(ManualClock(), script=lambda n, name: error if n == 2 else frame())
    async with recording(source, duration=0.4) as rec:
        batches = await collect(rec)
    assert len(batches) == 4
    failed = batches[1]["zpa"]
    assert failed.frame is None
    assert failed.error is error
    assert failed.channels == CHANNELS
    assert (failed.address, failed.protocol) == (1, ProtocolKind.MODBUS_RTU)
    assert failed.latency_s >= 0
    ok_row, error_row = sample_to_row(batches[0]["zpa"]), sample_to_row(failed)
    assert list(ok_row) == list(error_row)
    assert error_row["ch3_value"] is None
    assert error_row["error_type"] == "fujilib.errors.FujiModbusTimeoutError"
    assert rec.summary.error_samples == 1


async def test_every_analyzer_is_in_every_batch() -> None:
    def script(n: int, name: str) -> Outcome:
        if name == "b" and n == 2:
            return None  # the source left it out
        return frame()

    source = StubSource(ManualClock(), names=("a", "b"), script=script)
    async with recording(source, duration=0.3) as rec:
        batches = await collect(rec)
    assert [list(b) for b in batches] == [["a", "b"]] * 3
    missing = batches[1]["b"]
    assert missing.error is not None
    assert "no result for 'b'" in str(missing.error)
    assert missing.address == 2


async def test_names_choose_the_analyzers() -> None:
    source = StubSource(ManualClock(), names=("a", "b"))
    async with recording(source, duration=0.2, names=["b"]) as rec:
        batches = await collect(rec)
    assert [list(b) for b in batches] == [["b"], ["b"]]


# --- Overflow --------------------------------------------------------------------------------


async def test_block_waits_for_the_consumer_and_counts_ticks_missed_meanwhile() -> None:
    clock = ManualClock()
    source = StubSource(clock)
    async with recording(source, duration=1.0, buffer_size=1) as rec:
        batches: list[Batch] = []
        async for batch in rec:
            batches.append(batch)
            clock.advance(0.25)  # a slow consumer
    summary = rec.summary
    assert summary.samples_dropped == 0
    assert summary.samples_late > 0
    assert len(batches) == summary.samples_emitted
    assert summary.samples_emitted + summary.samples_late == 10


def numbered(n: int, name: str) -> Outcome:
    """A frame whose CH1 raw value is the poll number."""
    del name
    return frame((reading(ChannelId.CH1, Gas.CO2, n, 0),))


def poll_numbers(batches: list[Batch]) -> list[int]:
    numbers: list[int] = []
    for b in batches:
        f = b["zpa"].frame
        assert f is not None
        numbers.append(f.channel("CH1").raw_value)
    return numbers


async def _fill_then_read(policy: OverflowPolicy) -> tuple[list[Batch], AcquisitionSummary]:
    source = StubSource(ManualClock(), script=numbered)
    async with recording(source, duration=0.5, buffer_size=2, overflow=policy) as rec:
        while source.polls < 5:  # let the producer run ahead of the consumer
            await anyio.lowlevel.checkpoint()
        await anyio.lowlevel.checkpoint()
        batches = await collect(rec)
    return batches, rec.summary


async def test_drop_newest_keeps_the_first_batches() -> None:
    batches, summary = await _fill_then_read(OverflowPolicy.DROP_NEWEST)
    assert len(batches) == summary.samples_emitted == 2
    assert summary.samples_dropped == 3
    assert poll_numbers(batches) == [1, 2]
    assert summary.samples_emitted + summary.samples_dropped == 5


async def test_drop_oldest_keeps_the_last_batches() -> None:
    batches, summary = await _fill_then_read(OverflowPolicy.DROP_OLDEST)
    assert len(batches) == summary.samples_emitted == 2
    assert summary.samples_dropped == 3
    assert poll_numbers(batches) == [4, 5]


async def test_drop_oldest_sends_at_once_when_there_is_room() -> None:
    source = StubSource(ManualClock())
    async with recording(source, duration=0.3, overflow=OverflowPolicy.DROP_OLDEST) as rec:
        assert len(await collect(rec)) == 3
    assert rec.summary.samples_dropped == 0


# --- Disconnects -----------------------------------------------------------------------------


async def test_a_disconnect_ends_the_recording_after_its_batch() -> None:
    lost = FujiConnectionError("the port failed")
    source = StubSource(ManualClock(), script=lambda n, name: lost if n >= 3 else frame())
    batches: list[Batch] = []
    summaries: list[AcquisitionSummary] = []

    async def body() -> None:
        async with recording(source, duration=None) as rec:
            summaries.append(rec.summary)
            batches.extend(await collect(rec))

    with pytest.raises(FujiConnectionError) as info:
        await body()
    assert info.value is lost
    assert len(batches) == 3  # the stream ended after the failed tick was delivered
    assert batches[-1]["zpa"].error is lost
    assert summaries[0].finished_at is not None
    assert source.polls == 3


class Link:
    """A connection that fails at one poll and stays down until reopened."""

    def __init__(self, fails_at: int) -> None:
        self.fails_at = fails_at
        self.down = False
        self.lost = FujiConnectionError("the port failed")

    def script(self, n: int, name: str) -> Outcome:
        del name
        if n == self.fails_at:
            self.down = True
        return self.lost if self.down else frame()

    def reopened(self) -> None:
        self.down = False


async def test_a_reconnect_policy_rides_out_an_outage() -> None:
    # Down at poll 3 (0.2 s): attempts at 0.4 s (fails) and 0.5 s (succeeds).
    link = Link(fails_at=3)
    source = StubSource(
        ManualClock(),
        reopenable=True,
        script=link.script,
        reconnect_errors=[FujiConnectionError("still unplugged")],
        on_reconnect=link.reopened,
    )
    policy = ReconnectPolicy(backoff_s=(0.15, 0.1))
    async with recording(source, duration=1.0, reconnect=policy) as rec:
        batches = await collect(rec)
    assert len(batches) == 10
    failed = [b["zpa"].error is not None for b in batches]
    assert failed == [False, False, True, True, True, True, False, False, False, False]
    assert source.reconnects == ["zpa", "zpa"]
    summary = rec.summary
    assert (summary.disconnects, summary.reconnects, summary.error_samples) == (1, 1, 4)


async def test_a_source_that_recovers_by_itself_ends_the_outage() -> None:
    lost = FujiConnectionError("a moment's trouble")
    source = StubSource(
        ManualClock(), reopenable=True, script=lambda n, name: lost if n == 2 else frame()
    )
    policy = ReconnectPolicy(backoff_s=(5.0,))
    async with recording(source, duration=0.5, reconnect=policy) as rec:
        _ = await collect(rec)
    assert source.reconnects == []
    assert (rec.summary.disconnects, rec.summary.reconnects) == (1, 0)


async def test_reconnection_gives_up_after_max_attempts() -> None:
    lost = FujiConnectionError("the port failed")
    source = StubSource(
        ManualClock(),
        reopenable=True,
        script=lambda n, name: lost if n >= 2 else frame(),
        reconnect_errors=[FujiConnectionError("one"), FujiModbusTimeoutError("two")],
    )
    policy = ReconnectPolicy(backoff_s=(0.0,), max_attempts=2)
    summaries: list[AcquisitionSummary] = []

    async def body() -> None:
        async with recording(source, duration=None, reconnect=policy) as rec:
            summaries.append(rec.summary)
            _ = await collect(rec)

    with pytest.raises(FujiModbusTimeoutError, match="two"):
        await body()
    assert source.reconnects == ["zpa", "zpa"]
    assert summaries[0].reconnects == 0


async def test_another_analyzer_ends_the_recording_at_once() -> None:
    lost = FujiConnectionError("the port failed")
    other = FujiConfigurationError("not the analyzer that was open")
    source = StubSource(
        ManualClock(),
        reopenable=True,
        script=lambda n, name: lost if n >= 2 else frame(),
        reconnect_errors=[other],
    )
    with pytest.raises(FujiConfigurationError) as info:
        async with recording(source, duration=None, reconnect=ReconnectPolicy((0.0,))) as rec:
            _ = await collect(rec)
    assert info.value is other


def test_reconnect_policy() -> None:
    policy = ReconnectPolicy(backoff_s=(1.0, 2.0))
    assert [policy.delay(n) for n in (1, 2, 3, 9)] == [1.0, 2.0, 2.0, 2.0]
    assert ReconnectPolicy().backoff_s[0] == 0.5
    for bad in ((), (-1.0,), (float("nan"),), (True,)):
        with pytest.raises(FujiValidationError):
            _ = ReconnectPolicy(backoff_s=bad)
    for attempts in (0, -1, True, 1.5):
        with pytest.raises(FujiValidationError):
            _ = ReconnectPolicy(max_attempts=attempts)  # type: ignore[arg-type]


# --- The block -------------------------------------------------------------------------------


async def test_leaving_early_stops_the_recording() -> None:
    source = StubSource(ManualClock())
    async with recording(source, duration=None) as rec:
        while source.polls < 5:  # the producer runs ahead of the consumer
            await anyio.lowlevel.checkpoint()
        async for _batch in rec:
            break
    assert rec.summary.finished_at is not None
    # The batches left unread count as dropped: emitted is what was received.
    assert rec.summary.samples_emitted == 1
    assert rec.summary.samples_dropped >= 3  # the fifth poll may not be published yet
    polls = source.polls
    await anyio.lowlevel.checkpoint()
    assert source.polls == polls


async def test_an_error_in_the_block_is_raised_as_itself() -> None:
    source = StubSource(ManualClock())
    summaries: list[AcquisitionSummary] = []

    async def body() -> None:
        async with recording(source, duration=None) as rec:
            summaries.append(rec.summary)
            _ = await rec.stream.receive()
            msg = "mine"
            raise FujiValidationError(msg)

    with pytest.raises(FujiValidationError, match="mine"):
        await body()
    assert summaries[0].finished_at is not None


async def test_two_errors_stay_a_group() -> None:
    source = StubSource(ManualClock())

    async def fail() -> None:
        msg = "two"
        raise FujiValidationError(msg)

    async def body() -> None:
        async with recording(source, duration=None), anyio.create_task_group() as tg:
            _ = tg.start_soon(fail)
            msg = "one"
            raise FujiValidationError(msg)

    with pytest.raises(BaseExceptionGroup):
        await body()


async def test_a_failing_source_and_a_failing_block_stay_a_group() -> None:
    def script(n: int, name: str) -> Outcome:
        if n == 2:
            msg = "a bug in the source"
            raise RuntimeError(msg)
        return frame()

    source = StubSource(ManualClock(), script=script)

    async def body() -> None:
        async with recording(source, duration=None) as rec:
            try:
                _ = await rec.stream.receive()
                _ = await rec.stream.receive()
            finally:
                msg = "the block failed too"
                raise FujiValidationError(msg)

    with pytest.raises(BaseExceptionGroup) as info:
        await body()
    assert len(info.value.exceptions) == 2


# --- Arguments -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate_hz": 0},
        {"rate_hz": -1.0},
        {"rate_hz": float("inf")},
        {"rate_hz": True},
        {"duration": 0},
        {"duration": float("nan")},
        {"buffer_size": 0},
        {"buffer_size": 1.5},
        {"overflow": "sometimes"},
        {"names": "zpa"},
        {"names": ["other"]},
        {"reconnect": "yes"},
        {"reconnect": ReconnectPolicy()},  # the stub is not reopenable
    ],
)
async def test_bad_arguments_are_refused_before_polling(kwargs: dict[str, object]) -> None:
    source = StubSource(ManualClock())
    arguments: dict[str, object] = {"rate_hz": 10.0, "duration": 1.0, **kwargs}
    with pytest.raises(FujiValidationError):
        async with _record(
            source,
            rate_hz=arguments["rate_hz"],  # type: ignore[arg-type]
            duration=arguments["duration"],  # type: ignore[arg-type]
            names=arguments.get("names"),  # type: ignore[arg-type]
            overflow=arguments.get("overflow", OverflowPolicy.BLOCK),  # type: ignore[arg-type]
            buffer_size=arguments.get("buffer_size", 64),  # type: ignore[arg-type]
            reconnect=arguments.get("reconnect"),  # type: ignore[arg-type]
            clock=source.clock,
        ):
            pass
    assert source.polls == 0


async def test_an_analyzer_without_channels_is_refused() -> None:
    source = StubSource(ManualClock(), channels=())
    with pytest.raises(FujiValidationError, match="no established channels"):
        async with recording(source):
            pass


async def test_a_source_without_analyzers_is_refused() -> None:
    source = StubSource(ManualClock(), names=())
    with pytest.raises(FujiValidationError, match="no analyzers"):
        async with recording(source):
            pass


async def test_something_that_is_not_a_source_is_refused() -> None:
    with pytest.raises(FujiValidationError, match="poll source"):
        async with record(object(), rate_hz=1.0):  # type: ignore[arg-type]
            pass


# --- record() on the event loop, over the simulated analyzer ----------------------------------


@pytest.fixture
async def zpa() -> AsyncGenerator[PollSourceAdapter]:
    async with analyzer_on(bench()) as (anz, _line):
        yield PollSourceAdapter("zpa", anz)


async def test_record_polls_the_analyzer(zpa: PollSourceAdapter) -> None:
    async with record(zpa, rate_hz=20.0, duration=0.2) as rec:
        batches = await collect(rec)
    assert len(batches) + rec.summary.samples_late == 4
    sample = batches[0]["zpa"]
    assert sample.frame is not None
    assert sample.channels == CHANNELS
    assert sample.t_mono_ns == sample.frame.readings_timing.midpoint_mono_ns
    assert sample_to_row(sample)["ch3_gas"] == "o2"


async def test_a_hold_during_a_recording_shows_in_the_rows() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        source = PollSourceAdapter("zpa", anz)
        async with record(source, rate_hz=50.0, buffer_size=1) as rec:
            first = later = await rec.stream.receive()
            mock.set_register("status.ch3.hold", 1)
            for _ in range(2):  # one batch may have been polled before the change
                later = await rec.stream.receive()
    assert sample_to_row(first["zpa"])["ch3_state"] == "ok"
    row = sample_to_row(later["zpa"])
    assert (row["ch3_state"], row["ch3_hold"], row["ch3_valid"]) == ("hold", True, False)
    assert row["ch1_state"] == "ok"


async def test_a_channel_established_later_stays_out_of_the_rows() -> None:
    mock = bench()
    async with analyzer_on(mock, channel_map=None, identify=False) as (anz, _line):
        _ = await anz.identify()
        established = tuple(c.channel for c in anz.channels)
        source = PollSourceAdapter("zpa", anz)
        async with record(source, rate_hz=50.0, buffer_size=1) as rec:
            batch = await rec.stream.receive()
            mock.set_reading(ChannelId.CH4, 123, 1)
            for _ in range(2):  # one batch may have been polled before the change
                batch = await rec.stream.receive()
    sample = batch["zpa"]
    assert sample.channels == established
    assert sample.frame is not None
    assert ChannelId.CH4 in sample.frame.channels
    assert "ch4_value" not in sample_to_row(sample)


async def test_a_pulled_cable_ends_the_recording() -> None:
    async with replugging(bench()) as (anz, cable):
        batches: list[Batch] = []

        async def body() -> None:
            async with record(PollSourceAdapter("zpa", anz), rate_hz=50.0) as rec:
                async for batch in rec:
                    batches.append(batch)
                    if len(batches) == 2:
                        await cable.unplug()

        with pytest.raises(FujiConnectionError):
            await body()
        assert batches[-1]["zpa"].error is not None
        assert all(b["zpa"].error is None for b in batches[:2])


async def test_a_pulled_cable_is_ridden_out_with_a_reconnect_policy() -> None:
    mock = bench()
    async with replugging(mock) as (anz, cable):
        policy = ReconnectPolicy(backoff_s=(0.0,))
        source = PollSourceAdapter("zpa", anz)
        states: list[bool] = []
        with anyio.fail_after(20):  # a regression fails here instead of hanging
            async with record(source, rate_hz=50.0, reconnect=policy) as rec:
                async for batch in rec:
                    states.append(batch["zpa"].error is None)
                    if len(states) == 3:
                        await cable.unplug()
                    if len(states) == 6:
                        cable.replug()
                    if len(states) >= 6 and states[-1] and not states[-2]:
                        break
        assert states[:3] == [True, True, True]
        assert False in states
        assert states[-1]
        assert rec.summary.disconnects == 1
        assert rec.summary.reconnects == 1
        _ = await anz.poll()


async def test_the_poll_is_the_full_two_block_poll() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        async with record(PollSourceAdapter("zpa", anz), rate_hz=10.0, duration=0.1) as rec:
            _ = await collect(rec)
    assert mock.transactions() == POLL


async def test_a_timeout_is_an_error_row_not_the_end() -> None:
    mock = bench()
    async with analyzer_on(mock, read_retries=0, request_timeout=0.05) as (anz, _line):
        mock.inject(FaultKind.DROP, when=when(FC04, 0x0000, 61))
        async with record(PollSourceAdapter("zpa", anz), rate_hz=20.0, duration=0.15) as rec:
            batches = await collect(rec)
    errors = [b["zpa"].error for b in batches]
    assert isinstance(errors[0], FujiModbusTimeoutError)
    assert errors[1:] == [None] * (len(errors) - 1)
