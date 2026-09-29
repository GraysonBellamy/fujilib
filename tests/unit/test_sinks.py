"""Row schemas, the memory and CSV sinks, and :func:`pipe` (design §7.6)."""

from __future__ import annotations

import csv
import math
import time
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fujilib import (
    CsvSink,
    FujiError,
    FujiModbusTimeoutError,
    FujiSinkError,
    FujiSinkSchemaError,
    FujiSinkWriteError,
    FujiValidationError,
    InMemorySink,
    ProtocolKind,
    Recording,
    Sample,
    SampleSink,
    pipe,
    sample_to_row,
)
from fujilib.registry.channels import ChannelId, Gas
from fujilib.sinks import BaseSink, SchemaLock, row_columns, sample_channels
from fujilib.sinks.csv import csv_cell
from fujilib.streaming.recorder import AcquisitionSummary, Batch
from tests.factories import analyzer, bench_readings, frame, reading, timing

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from fujilib.devices.models import Scalar

pytestmark = pytest.mark.anyio

CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)


def ok(device: str = "zpa", channels: tuple[ChannelId, ...] = CHANNELS) -> Sample:
    return Sample.from_frame(frame(), device=device, address=1, channels=channels)


def failed(device: str = "zpa", channels: tuple[ChannelId, ...] = CHANNELS) -> Sample:
    return Sample.from_error(
        FujiModbusTimeoutError("no reply"),
        device=device,
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        timing=timing(1000.0),
        channels=channels,
    )


def read_csv(path: Path) -> list[dict[str, str | None]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file, quoting=csv.QUOTE_NOTNULL))


def parse_cell(text: str | None, python_type: type) -> Scalar:
    """What :func:`csv_cell` wrote, read back as the column's type."""
    if text is None:
        return None
    if python_type is bool:
        return {"true": True, "false": False}[text]
    if python_type is int:
        return int(text)
    if python_type is float:
        return float(text)
    return text


# --- Rows ------------------------------------------------------------------------------------


def test_a_sample_row_uses_the_samples_channels() -> None:
    error = failed()
    assert sample_channels(error) == CHANNELS
    assert list(sample_to_row(error)) == [c.name for c in row_columns(CHANNELS)]
    assert list(sample_to_row(error)) == list(sample_to_row(ok()))


def test_a_sample_made_without_channels_uses_its_frames() -> None:
    sample = Sample.from_frame(frame(), device="zpa", address=1)
    assert sample.channels == CHANNELS  # from_frame defaults them
    bare = Sample(
        device="zpa",
        address=1,
        frame=frame(),
        protocol=ProtocolKind.MODBUS_RTU,
        t_mono_ns=1,
        t_utc=timing().requested_at,
        requested_at=timing().requested_at,
        received_at=timing().received_at,
        latency_s=0.02,
    )
    assert sample_channels(bare) == CHANNELS
    assert sample_channels(failed(channels=())) == ()


# --- SchemaLock -----------------------------------------------------------------------------


def test_the_schema_locks_to_the_first_batch() -> None:
    lock = SchemaLock("test")
    assert not lock.is_locked
    assert (lock.channels, lock.columns) == ((), ())
    co2_only = Sample.from_frame(frame(bench_readings()[:1]), device="zpa", address=1)
    rows = lock.rows([failed(channels=(ChannelId.CH3,)), co2_only])
    assert lock.channels == (ChannelId.CH1, ChannelId.CH3)
    assert list(rows[0]) == [c.name for c in lock.columns]
    assert rows[1]["ch3_value"] is None  # a channel the sample lacks
    assert lock.lock([ChannelId.CH3, ChannelId.CH1]) == lock.columns


def test_a_sample_with_a_channel_the_schema_lacks_is_refused() -> None:
    lock = SchemaLock("test", [ChannelId.CH1])
    with pytest.raises(FujiSinkSchemaError, match="CH2, CH3"):
        _ = lock.rows([ok()])
    with pytest.raises(FujiSinkSchemaError, match="locked to CH1"):
        _ = lock.lock(CHANNELS)


def test_a_schema_with_no_channels() -> None:
    lock = SchemaLock("test", [])
    with pytest.raises(FujiSinkSchemaError, match="no channels"):
        _ = lock.rows([ok()])


# --- BaseSink and InMemorySink -----------------------------------------------------------------


def is_open(sink: BaseSink) -> bool:
    return sink.is_open


async def test_the_sink_lifecycle() -> None:
    sink = InMemorySink()
    assert isinstance(sink, SampleSink)
    with pytest.raises(FujiSinkError, match="needs an open sink"):
        await sink.write_many([ok()])
    async with sink:
        await sink.open()  # again: no-op
        assert is_open(sink)
        await sink.write_many([])
        await sink.write_many([ok(), failed()])
    await sink.close()  # again: no-op
    assert not is_open(sink)
    assert len(sink.samples) == 2
    assert [r["error_type"] for r in sink.rows()] == [
        None,
        "fujilib.errors.FujiModbusTimeoutError",
    ]
    with pytest.raises(FujiSinkError, match="was closed"):
        await sink.open()
    assert "closed" in repr(sink)


async def test_closing_a_sink_never_opened() -> None:
    sink = InMemorySink()
    await sink.close()
    with pytest.raises(FujiSinkError):
        await sink.open()


async def test_a_bare_base_sink_cannot_write() -> None:
    async with BaseSink("bare") as sink:
        with pytest.raises(NotImplementedError):
            await sink.write_many([ok()])


async def test_an_error_first_memory_sink_keeps_typed_rows() -> None:
    async with InMemorySink() as sink:
        await sink.write_many([failed()])
        await sink.write_many([ok()])
    first, second = sink.rows()
    assert first["ch3_value"] is None
    assert isinstance(second["ch3_value"], float)
    assert [c.python_type for c in sink.schema.columns if c.name == "ch3_value"] == [float]


# --- CsvSink ----------------------------------------------------------------------------------


def test_csv_cells() -> None:
    assert [csv_cell(v) for v in (None, True, False, 0.1, 3, "x", "")] == [
        None,
        "true",
        "false",
        0.1,
        3,
        "x",
        "",
    ]


async def test_csv_writes_a_header_and_rows(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "run.csv"
    sink = CsvSink(path)
    async with sink:
        await sink.write_many([failed()])
        await sink.write_many([ok(), ok()])
    assert sink.path == path
    rows = read_csv(path)
    assert list(rows[0]) == [c.name for c in row_columns(CHANNELS)]
    assert len(rows) == 3
    assert rows[0]["ch3_value"] is None
    assert rows[0]["ch3_errors"] is None  # unknown
    assert rows[1]["ch3_value"] == "20.29"
    assert rows[1]["ch3_valid"] == "true"
    assert rows[1]["ch3_errors"] == ""  # no errors
    assert rows[1]["error_type"] is None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith('"zpa",1,"modbus_rtu",')


async def test_csv_with_channels_writes_its_header_at_open(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    async with CsvSink(path, channels=CHANNELS):
        pass
    assert path.read_text(encoding="utf-8").startswith('"device","address","protocol",')
    assert read_csv(path) == []


async def test_csv_replaces_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "run.csv"
    path.write_text("old\n", encoding="utf-8")
    async with CsvSink(path) as sink:
        await sink.write_many([ok()])
    assert len(read_csv(path)) == 1


async def test_csv_closes_the_file_when_the_header_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = CsvSink(tmp_path / "h.csv", channels=CHANNELS)

    def refuse(rows: object) -> None:
        del rows
        raise OSError("disk full")

    monkeypatch.setattr(sink, "_write_rows", refuse)
    with pytest.raises(FujiSinkWriteError, match="disk full"):
        await sink.open()
    assert sink._file is None


async def test_csv_cannot_open_a_directory(tmp_path: Path) -> None:
    with pytest.raises(FujiSinkWriteError, match="cannot open"):
        async with CsvSink(tmp_path):
            pass


class _BrokenFile:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on

    def write(self, text: str) -> int:
        if self.fail_on == "write":
            raise OSError("disk full")
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self.fail_on == "close":
            raise OSError("lost")


async def test_csv_write_and_close_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = CsvSink(tmp_path / "a.csv")
    await sink.open()
    real = sink._file
    assert real is not None
    real.close()
    monkeypatch.setattr(sink, "_file", _BrokenFile("write"))
    with pytest.raises(FujiSinkWriteError, match="cannot write"):
        await sink.write_many([ok()])
    monkeypatch.setattr(sink, "_file", _BrokenFile("close"))
    with pytest.raises(FujiSinkWriteError, match="cannot finish"):
        await sink.close()


# --- pipe ------------------------------------------------------------------------------------


class CountingSink(InMemorySink):
    """Counts its writes; can be told to fail or to be slow."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        super().__init__()
        self.writes: list[int] = []
        self.fail = fail
        self.delay = delay
        self.written = anyio.Event()

    async def write_many(self, samples: Sequence[Sample]) -> None:
        if self.delay:
            await anyio.sleep(self.delay)
        if self.fail:
            msg = "refused"
            raise FujiSinkWriteError(msg)
        self.writes.append(len(samples))
        self.written.set()
        await super().write_many(samples)


def batch(*samples: Sample) -> Batch:
    return {s.device: s for s in samples}


async def test_pipe_writes_a_whole_stream_in_groups() -> None:
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=16)
    with send:
        for _ in range(5):
            send.send_nowait(batch(ok("a"), ok("b")))
        send.send_nowait(batch(failed("a"), ok("b")))
    async with CountingSink() as sink:
        with receive:
            summary = await pipe(receive, sink, batch_size=4)
    assert sink.writes == [4, 4, 4]
    assert (summary.samples_emitted, summary.error_samples) == (6, 1)
    assert summary.finished_at is not None
    assert sum(r["error_type"] is not None for r in sink.rows()) == 1


async def test_pipe_takes_a_recording() -> None:
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=4)
    with send:
        send.send_nowait(batch(ok()))
    summary = AcquisitionSummary(started_at=timing().requested_at)
    async with CountingSink() as sink:
        with receive:
            _ = await pipe(Recording(stream=receive, summary=summary, rate_hz=1.0), sink)
    assert sink.writes == [1]


async def test_pipe_flushes_on_time_while_the_stream_is_idle() -> None:
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=4)
    async with CountingSink() as sink, anyio.create_task_group() as tg:
        _ = tg.start_soon(partial(pipe, receive, sink, flush_interval=0.05))
        await send.send(batch(ok()))
        with anyio.fail_after(2):
            await sink.written.wait()
        await send.aclose()
    receive.close()
    assert sink.writes == [1]


async def test_pipe_writes_what_it_holds_when_cancelled() -> None:
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=4)
    async with CountingSink() as sink:
        with anyio.move_on_after(0.1):
            async with send:
                await send.send(batch(ok()))
                await send.send(batch(ok()))
                _ = await pipe(receive, sink, batch_size=64, flush_interval=60)
    assert sink.writes == [2]
    receive.close()


async def test_pipe_takes_the_batches_waiting_when_cancelled() -> None:
    # The recorder had put three batches on the stream; the pipe was cancelled
    # before it took them. Every one is still written.
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=8)
    for _ in range(3):
        send.send_nowait(batch(ok()))
    async with CountingSink() as sink:
        with anyio.CancelScope() as scope:
            scope.cancel()
            _ = await pipe(receive, sink, batch_size=64, flush_interval=60)
    assert sink.writes == [3]
    send.close()
    receive.close()


async def test_pipe_raises_when_its_last_write_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fujilib.sinks import base

    monkeypatch.setattr(base, "_FINAL_WRITE_S", 0.01)
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=4)
    with send:
        send.send_nowait(batch(ok()))
    async with CountingSink(delay=0.2) as sink:
        with pytest.raises(FujiSinkWriteError, match="last 1 samples"):
            _ = await pipe(receive, sink, batch_size=64, flush_interval=60)
    receive.close()


async def test_pipe_needs_a_recording_or_its_stream() -> None:
    with pytest.raises(FujiValidationError, match="Recording or its stream"):
        _ = await pipe(object(), InMemorySink())  # type: ignore[arg-type]


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_native_cancel_waits_for_a_write_in_its_thread() -> None:
    # asyncio's runner cancels the main task natively on Ctrl-C; the thread's
    # write must still be over before the cancellation reaches the caller.
    import asyncio

    from fujilib.sinks._thread import in_thread

    finished: list[float] = []

    def slow() -> None:
        time.sleep(0.2)
        finished.append(time.monotonic())

    task = asyncio.create_task(in_thread(slow))
    await asyncio.sleep(0.05)
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(finished) == 1


async def test_a_thread_error_is_raised_as_itself() -> None:
    from fujilib.sinks._thread import in_thread

    def fail() -> None:
        msg = "disk"
        raise OSError(msg)

    with pytest.raises(OSError, match="disk"):
        await in_thread(fail)


async def test_pipe_does_not_retry_a_failed_sink() -> None:
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=4)
    sink = CountingSink(fail=True)
    with send:
        send.send_nowait(batch(ok()))
        send.send_nowait(batch(ok()))
    with pytest.raises(FujiSinkWriteError):
        _ = await pipe(receive, sink, batch_size=1)
    assert sink.writes == []
    receive.close()


async def test_pipe_gives_up_on_a_final_write_that_takes_too_long(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fujilib.sinks import base

    monkeypatch.setattr(base, "_FINAL_WRITE_S", 0.01)
    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=4)
    async with CountingSink(delay=0.2) as sink:
        with anyio.move_on_after(0.05):
            async with send:
                await send.send(batch(ok()))
                _ = await pipe(receive, sink, batch_size=64, flush_interval=60)
    assert "could not write the last 1 samples" in caplog.text
    receive.close()


@pytest.mark.parametrize(
    ("batch_size", "flush_interval"),
    [(0, 1.0), (True, 1.0), (1.5, 1.0), (1, 0), (1, -1.0), (1, math.inf), (1, "1")],
)
async def test_pipe_arguments(batch_size: Any, flush_interval: Any) -> None:
    send, receive = anyio.create_memory_object_stream[Batch]()
    with send, receive, pytest.raises(FujiValidationError):
        _ = await pipe(
            receive, InMemorySink(), batch_size=batch_size, flush_interval=flush_interval
        )


# --- Round trips ------------------------------------------------------------------------------

_readings = st.tuples(
    st.integers(-9999, 9999), st.integers(-9999, 9999), st.integers(-9999, 9999)
).map(
    lambda raws: (
        reading(ChannelId.CH1, Gas.CO2, raws[0], 2),
        reading(ChannelId.CH2, Gas.CO, raws[1], 3),
        reading(ChannelId.CH3, Gas.O2, raws[2], 2),
    )
)
_samples = st.one_of(
    _readings.map(
        lambda rs: Sample.from_frame(
            frame(rs, analyzer(instrument_error=rs[0].raw_value < 0)),
            device="zpa",
            address=1,
            channels=CHANNELS,
        )
    ),
    st.just(failed()),
    st.just(
        Sample.from_error(
            FujiError('odd, text\nwith "quotes"'),
            device="zpa",
            address=1,
            protocol=ProtocolKind.MODBUS_RTU,
            timing=timing(),
            channels=CHANNELS,
        )
    ),
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(samples=st.lists(_samples, min_size=1, max_size=8))
def test_csv_reads_back_exactly(tmp_path: Path, samples: list[Sample]) -> None:
    path = tmp_path / "round.csv"

    async def write() -> None:
        async with CsvSink(path) as sink:
            await sink.write_many(samples)

    anyio.run(write)
    columns = row_columns(CHANNELS)
    back = [
        {c.name: parse_cell(row[c.name], c.python_type) for c in columns} for row in read_csv(path)
    ]
    assert back == [sample_to_row(s) for s in samples]
