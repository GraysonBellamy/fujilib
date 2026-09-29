"""Blocking recording and sinks (design §7.3, §7.6)."""

from __future__ import annotations

import _thread
import csv
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from fujilib import (
    FujiSinkError,
    FujiSinkWriteError,
    FujiValidationError,
    PollSourceAdapter,
    Sample,
    sample_to_row,
)
from fujilib.sync import (
    Fuji,
    SyncCsvSink,
    SyncInMemorySink,
    SyncParquetSink,
    SyncPortal,
    SyncRecording,
    pipe,
    record,
)
from fujilib.sync import PollSourceAdapter as SyncPollSourceAdapter
from fujilib.testing import mock_transport
from tests.facade import POLL, bench
from tests.factories import read_parquet

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from fujilib.streaming.recorder import AcquisitionSummary
    from fujilib.sync import SyncAnalyzer
    from fujilib.testing import MockAnalyzer


@contextmanager
def analyzer(mock: MockAnalyzer | None = None) -> Generator[SyncAnalyzer]:
    with (
        SyncPortal() as portal,
        portal.wrap_async_context_manager(mock_transport(mock or bench())) as (transport, _line),
        Fuji.open(
            transport, channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}, portal=portal
        ) as anz,
    ):
        yield anz


def test_a_blocking_recording() -> None:
    mock = bench()
    with analyzer(mock) as anz:
        source = SyncPollSourceAdapter("zpa", anz)
        assert (source.name, source.device, source.portal) == ("zpa", anz, anz.portal)
        assert list(source.layout()) == ["zpa"]
        mock.clear()
        assert source.poll()["zpa"].ok
        assert mock.transactions() == POLL
        with record(source, rate_hz=20.0, duration=0.2) as rec:
            assert isinstance(rec, SyncRecording)
            assert rec.stream is rec
            assert rec.rate_hz == 20.0
            assert rec.portal is anz.portal
            batches = list(rec)
        assert rec.recording.summary is rec.summary
    assert len(batches) + rec.summary.samples_late == 4
    assert rec.summary.finished_at is not None
    assert sample_to_row(batches[0]["zpa"])["ch3_gas"] == "o2"


def test_pipe_to_blocking_sinks(tmp_path: Path) -> None:
    with analyzer() as anz:
        source = SyncPollSourceAdapter("zpa", anz)
        with (
            record(source, rate_hz=20.0, duration=0.15) as rec,
            SyncCsvSink(tmp_path / "a.csv", portal=anz.portal) as sink,
        ):
            summary = pipe(rec, sink)
        with (
            record(source, rate_hz=20.0, duration=0.15) as rec2,
            SyncParquetSink(tmp_path / "a.parquet", metadata={"k": "v"}) as parquet,
        ):
            _ = pipe(rec2, parquet)
        memory = SyncInMemorySink(channels=[c.channel for c in anz.channels])
        with memory, record(source, rate_hz=20.0, duration=0.1) as rec3:
            _ = pipe(rec3, memory.async_sink)
    with (tmp_path / "a.csv").open(encoding="utf-8", newline="") as file:
        assert len(list(csv.DictReader(file))) == summary.samples_emitted
    assert len(read_parquet(tmp_path / "a.parquet")) == rec2.summary.samples_emitted
    assert len(memory.samples) == len(memory.rows()) == rec3.summary.samples_emitted


def test_leaving_the_loop_early_stops_the_recording() -> None:
    with analyzer() as anz, record(SyncPollSourceAdapter("zpa", anz), rate_hz=50.0) as rec:
        for _batch in rec:
            break
    assert rec.summary.finished_at is not None


def test_an_async_source_needs_its_portal() -> None:
    with analyzer() as anz:
        source = PollSourceAdapter("zpa", anz.analyzer)
        with pytest.raises(FujiValidationError, match="portal"), record(source, rate_hz=1.0):
            pass
        with record(source, rate_hz=20.0, duration=0.05, portal=anz.portal) as rec:
            assert len(list(rec)) + rec.summary.samples_late == 1


def test_a_blocking_reconnect_goes_through_the_analyzer() -> None:
    from fujilib import FujiConfigurationError

    with analyzer() as anz:
        source = SyncPollSourceAdapter("zpa", anz)
        with pytest.raises(FujiConfigurationError, match="caller supplied"):
            source.reconnect("zpa")


def test_a_sink_with_a_portal_of_its_own(tmp_path: Path) -> None:
    with analyzer() as anz:
        frame = SyncPollSourceAdapter("zpa", anz).poll()["zpa"].value
    assert frame is not None
    sink = SyncCsvSink(tmp_path / "own.csv")
    with sink:
        sink.write_many([Sample.from_frame(frame, device="zpa", address=1)])
    assert len((tmp_path / "own.csv").read_text(encoding="utf-8").splitlines()) == 2
    sink.close()  # again: no-op


def test_a_failed_open_stops_the_sinks_own_portal(tmp_path: Path) -> None:
    sink = SyncCsvSink(tmp_path)  # a directory: opening fails
    with pytest.raises(FujiSinkWriteError):
        sink.open()
    assert sink._portal is None
    sink.close()


def test_a_failed_open_keeps_a_shared_portal(tmp_path: Path) -> None:
    with SyncPortal() as portal:
        sink = SyncCsvSink(tmp_path, portal=portal)
        with pytest.raises(FujiSinkWriteError):
            sink.open()
        assert portal.running
        assert sink._portal is portal


def test_a_process_whose_sink_failed_to_open_can_exit(tmp_path: Path) -> None:
    # A portal left running would keep the interpreter from exiting.
    script = (
        "import sys\n"
        "from fujilib.sync import SyncCsvSink\n"
        "for _ in range(3):\n"
        "    try:\n"
        "        SyncCsvSink(sys.argv[1]).open()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=60, check=False)
    assert done.returncode == 0


def test_closing_after_the_shared_portal_stopped_is_an_error(tmp_path: Path) -> None:
    with SyncPortal() as portal:
        sink = SyncParquetSink(tmp_path / "late.parquet", portal=portal)
        sink.open()
    with pytest.raises(FujiSinkError, match="stopped before the sink was closed"):
        sink.close()
    sink.close()  # again: no-op


def test_ctrl_c_stops_a_blocking_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    from fujilib.sync import recording

    monkeypatch.setattr(recording, "_WAKE_S", 0.05)
    summaries: list[AcquisitionSummary] = []
    with analyzer() as anz:
        memory = SyncInMemorySink(portal=anz.portal)
        timer = threading.Timer(0.4, _thread.interrupt_main)

        def body() -> None:
            with memory, record(SyncPollSourceAdapter("zpa", anz), rate_hz=20.0) as rec:
                summaries.append(rec.summary)
                timer.start()
                _ = pipe(rec, memory)

        with pytest.raises(KeyboardInterrupt):
            body()
        timer.cancel()
    assert len(memory.samples) == summaries[0].samples_emitted > 0


def test_a_blocking_reopen() -> None:
    from fujilib import FujiConfigurationError

    with analyzer() as anz, pytest.raises(FujiConfigurationError, match="caller supplied"):
        _ = anz.reopen()
