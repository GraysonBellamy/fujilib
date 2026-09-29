"""Recording, sinks and the recording commands against a connected ZP analyzer (design §7.6).

Read-only: every request is a read. Gated as every hardware test is
(``conftest.py``). Each recording is a few seconds; the 12-hour recording and
the unplug test are procedures, not tests (``docs/hardware-test-day.md``).
"""

from __future__ import annotations

import csv
import itertools
import json
from typing import TYPE_CHECKING

import pytest

from fujilib import (
    CsvSink,
    ParquetSink,
    PollSourceAdapter,
    open_device,
    pipe,
    record,
    sample_to_row,
)
from fujilib.cli import capture, diag, stream
from fujilib.sinks import row_columns
from tests.factories import read_parquet

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from fujilib import Analyzer

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]


@pytest.fixture
async def analyzer(hardware_port: str, hardware_address: int) -> AsyncGenerator[Analyzer]:
    async with await open_device(hardware_port, address=hardware_address) as anz:
        yield anz


async def test_a_recording_at_5_hz(analyzer: Analyzer) -> None:
    source = PollSourceAdapter("zpa", analyzer)
    channels = tuple(c.channel for c in analyzer.channels)
    async with record(source, rate_hz=5.0, duration=4.0) as rec:
        batches = [batch async for batch in rec]
    summary = rec.summary
    assert summary.samples_emitted + summary.samples_late == 20
    # A poll takes about 0.12 s of the 0.2 s period, so a slow one now and then
    # (a retry, the scheduler) makes a tick late; most are on time.
    assert summary.samples_late <= 4
    assert summary.error_samples == 0
    samples = [b["zpa"] for b in batches]
    assert all(s.frame is not None and s.channels == channels for s in samples)
    stamps = [s.t_mono_ns for s in samples]
    assert stamps == sorted(stamps)
    gaps_s = [(b - a) / 1e9 for a, b in itertools.pairwise(stamps)]
    longest = 0.2 * (summary.samples_late + 1) + 0.15  # every skipped slot in one gap
    assert all(0.05 < g < longest for g in gaps_s), gaps_s
    keys = {tuple(sample_to_row(s)) for s in samples}
    assert keys == {tuple(c.name for c in row_columns(channels))}


async def test_pipe_to_csv_and_parquet(analyzer: Analyzer, tmp_path: Path) -> None:
    source = PollSourceAdapter("zpa", analyzer)
    channels = [c.channel for c in analyzer.channels]
    async with (
        CsvSink(tmp_path / "run.csv", channels=channels) as sink,
        record(source, rate_hz=2.0, duration=3.0) as rec,
    ):
        _ = await pipe(rec, sink)
    with (tmp_path / "run.csv").open(encoding="utf-8", newline="") as file:
        assert len(list(csv.DictReader(file))) == rec.summary.samples_emitted
    async with (
        ParquetSink(tmp_path / "run.parquet", channels=channels) as sink,
        record(source, rate_hz=2.0, duration=3.0) as rec,
    ):
        _ = await pipe(rec, sink)
    rows = read_parquet(tmp_path / "run.parquet")
    assert len(rows) == rec.summary.samples_emitted
    assert all(r["error_type"] is None for r in rows)


def test_fuji_stream(
    capsys: pytest.CaptureFixture[str], hardware_port: str, hardware_address: int
) -> None:
    argv = [hardware_port, "--address", str(hardware_address), "--rate", "2", "--duration", "2"]
    code = stream.main(argv)
    out, err = capsys.readouterr()
    assert code == 0
    assert len(out.splitlines()) >= 3
    assert "failed_polls: 0" in err


def test_fuji_capture(
    capsys: pytest.CaptureFixture[str], hardware_port: str, hardware_address: int, tmp_path: Path
) -> None:
    out = tmp_path / "cap.parquet"
    argv = [hardware_port, "--address", str(hardware_address), "--rate", "2", "--duration", "3"]
    argv += ["--out", str(out), "--quiet"]
    assert capture.main(argv) == 0
    _ = capsys.readouterr()
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["state"] == "finished"
    assert document["summary"]["failed_polls"] == 0
    assert len(read_parquet(out)) == document["summary"]["polls"]


def test_fuji_diag_timing(
    capsys: pytest.CaptureFixture[str], hardware_port: str, hardware_address: int, tmp_path: Path
) -> None:
    out = tmp_path / "timing.json"
    argv = ["timing", hardware_port, "--address", str(hardware_address), "--trials", "10"]
    argv += ["--gaps-ms", "1,5", "--out", str(out)]
    assert diag.main(argv) == 0
    _ = capsys.readouterr()
    document = json.loads(out.read_text(encoding="utf-8"))
    trials = document["trials"]
    assert len(trials) == 80
    # The analyzer needs at most 1 ms after any reply (design §2.4); allow one
    # background loss (about 1 in 3,000 requests).
    assert sum(bool(t["failed"]) for t in trials) <= 1
