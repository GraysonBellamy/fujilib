"""``fuji-stream``, ``fuji-capture`` and ``fuji-diag``, run in-process (design §7.7).

They run against the bundled bench bank with ``--fixture bench``, through
``open_device`` (or, for ``fuji-diag``, a Modbus port) and the simulated
analyzer. Ctrl-C is simulated with a real SIGINT, which asyncio's runner
turns into cancellation of the command, as it does at a terminal.
"""

from __future__ import annotations

import csv
import dataclasses
import errno
import io
import json
import signal
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as real_version
from typing import TYPE_CHECKING, Any

import pytest
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from fujilib import (
    DeviceResult,
    FujiConnectionError,
    FujiError,
    FujiFrameError,
    FujiModbusError,
    FujiModbusIllegalDataAddressError,
    FujiModbusTimeoutError,
    ProtocolKind,
    Sample,
)
from fujilib.cli import _common, _recording, capture, diag, stream
from fujilib.cli.stream import text_line
from fujilib.errors import ErrorContext, FujiConfigurationError
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.enums import AlarmState, ErrorCode
from fujilib.sinks import row_columns
from fujilib.streaming.poll_source import PollSourceAdapter
from fujilib.testing import FaultKind, mock_analyzer_pair
from fujilib.transport.serial import SerialTransport
from tests.factories import analyzer, frame, parquet_metadata, read_parquet, reading, timing

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from fujilib.devices.models import Frame
    from fujilib.transport.base import SerialSettings

BENCH = ["--fixture", "bench"]
ASSERT = ["--gas", "CH1=co2", "--gas", "CH2=co", "--gas", "CH3=o2"]
FAST = ["--rate", "20", "--duration", "0.25"]
CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)


def run(capsys: pytest.CaptureFixture[str], main: Callable[[list[str]], int], *argv: str) -> Any:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


class Interrupting(PollSourceAdapter):
    """Sends this process SIGINT (Ctrl-C) during poll ``at``."""

    at = 3

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.polls = 0

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        self.polls += 1
        if self.polls == self.at:
            signal.raise_signal(signal.SIGINT)
        return await super().poll(names)


class Unplugging(PollSourceAdapter):
    """Reports a connection failure from poll 3 on."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.polls = 0

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        self.polls += 1
        if self.polls >= 3:
            failure: DeviceResult[Frame] = DeviceResult(None, FujiConnectionError("unplugged"))
            return {self.name: failure}
        return await super().poll(names)


# --- fuji-stream ------------------------------------------------------------------------------


def test_stream_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, stream.main, *BENCH, *ASSERT, *FAST)
    assert code == 0
    lines = out.splitlines()
    assert lines
    # The bench bank's block capture: CO2 -0.10, CO -0.007, O2 20.18 vol%.
    assert all(
        x.endswith(" zpa  CH1 co2 -0.10 vol% ok  CH2 co -0.007 vol% ok  CH3 o2 20.18 vol% ok")
        for x in lines
    )
    assert "polls:" in err


def test_stream_as_csv(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys, stream.main, *BENCH, *ASSERT, *FAST, "--format", "csv")
    assert code == 0
    rows = list(csv.DictReader(io.StringIO(out), quoting=csv.QUOTE_NOTNULL))
    assert rows
    assert list(rows[0]) == [c.name for c in row_columns(CHANNELS)]
    assert rows[0]["ch3_value"] == "20.18"
    assert rows[0]["ch3_errors"] == ""


def test_stream_as_json_lines_named(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*BENCH, *ASSERT, *FAST, "--format", "jsonl", "--name", "cone", "--buffer-size", "8"]
    code, out, _err = run(capsys, stream.main, *argv)
    assert code == 0
    rows = [json.loads(line) for line in out.splitlines()]
    assert {r["device"] for r in rows} == {"cone"}
    assert rows[0]["ch3_value"] == 20.18


def test_stream_stops_cleanly_on_ctrl_c(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "PollSourceAdapter", Interrupting)
    code, out, err = run(capsys, stream.main, *BENCH, *ASSERT, "--rate", "20")
    assert code == 0
    assert "stopped by Ctrl-C" in err
    assert "polls:" in err
    assert len(out.splitlines()) >= 2


def interrupt_now(*args: object) -> str:
    """Ctrl-C before the recording starts."""
    del args
    signal.raise_signal(signal.SIGINT)
    return "zpa"


def test_stream_stopped_before_it_records(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _common.open_from_args

    def interrupting(args: Any) -> Any:
        signal.raise_signal(signal.SIGINT)
        return real(args)

    monkeypatch.setattr(stream, "open_from_args", interrupting)
    code, _out, err = run(capsys, stream.main, *BENCH, *ASSERT)
    assert code == 0
    assert "stopped by Ctrl-C" in err
    assert "polls:" not in err


@pytest.mark.parametrize("error", [BrokenPipeError(), OSError(errno.EINVAL, "closed")])
def test_stream_into_a_closed_pipe(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    class Closed(io.StringIO):
        def write(self, text: str) -> int:
            raise error

    monkeypatch.setattr(sys, "stdout", Closed())
    assert stream.main([*BENCH, *ASSERT, *FAST]) == 0


def test_another_output_error_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    class Full(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(sys, "stdout", Full())
    with pytest.raises(OSError, match="no space"):
        _ = stream.main([*BENCH, *ASSERT, *FAST])


def test_stream_into_head_exits_quietly() -> None:
    # A real pipe whose reader stops after one line, as in `fuji-stream ... | head -1`.
    argv = [sys.executable, "-m", "fujilib.cli.stream", *BENCH, *ASSERT, "--rate", "20"]
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        assert child.stdout is not None
        assert child.stderr is not None
        assert child.stdout.readline()
        child.stdout.close()
        code = child.wait(timeout=60)
        err = child.stderr.read()
    assert code == 0, err
    assert b"Traceback" not in err


def test_stream_ends_with_a_connection_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "PollSourceAdapter", Unplugging)
    code, out, err = run(capsys, stream.main, *BENCH, *ASSERT, "--rate", "20")
    assert code == 1
    assert "error: unplugged" in err
    assert "failed: FujiConnectionError: unplugged" in out


@pytest.mark.parametrize("command", ["stream", "capture"])
def test_a_fixture_cannot_be_reconnected(
    capsys: pytest.CaptureFixture[str], command: str, tmp_path: Path
) -> None:
    out = tmp_path / "a.csv"
    main, extra = (stream.main, []) if command == "stream" else (capture.main, ["--out", str(out)])
    with pytest.raises(SystemExit) as info:
        _ = main([*BENCH, *FAST, "--reconnect", *extra])
    assert info.value.code == 2
    assert "cannot be reopened" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    "argv",
    [
        [*BENCH, "--rate", "0"],
        [*BENCH, "--rate", "50"],
        [*BENCH, "--rate", "fast"],
        [*BENCH, "--duration", "-1"],
        [*BENCH, "--duration", "soon"],
        [*BENCH, "--buffer-size", "0"],
        [*BENCH, "--buffer-size", "x"],
        [*BENCH, "--overflow", "sometimes"],
        [],
    ],
)
def test_stream_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _ = stream.main(argv)
    assert info.value.code == 2
    _ = capsys.readouterr()


def sample_of(f: Frame) -> Sample:
    return Sample.from_frame(f, device="zpa", address=1, channels=(*CHANNELS, ChannelId.CH4))


def test_text_lines() -> None:
    readings = (
        reading(ChannelId.CH1, Gas.CO2, -11, 2),
        reading(ChannelId.CH2, Gas.CO, -9, 3),
        reading(ChannelId.CH3, Gas.O2, 2029, 2),
    )
    flagged = analyzer(
        errors=(ErrorCode.LIGHT_SOURCE,),
        alarms=(AlarmState.NONE, AlarmState.HIGH, *(AlarmState.NONE,) * 4),
        auto_calibration=True,
    )
    line = text_line(sample_of(frame(readings, flagged)))
    assert "CH4 -" in line
    assert line.endswith("[error 1, alarm 2, auto calibration]")
    blank = reading(ChannelId.CH1, Gas.CO2, -11, 2)
    unknown = frame((dataclasses.replace(blank, value=None),), detail=False)
    assert "CH1 co2 - vol%" in text_line(sample_of(unknown))
    error = Sample.from_error(
        FujiModbusTimeoutError("no reply"),
        device="zpa",
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        timing=timing(),
        channels=CHANNELS,
    )
    assert text_line(error).endswith("zpa  failed: FujiModbusTimeoutError: no reply")


# --- fuji-capture -----------------------------------------------------------------------------


def test_capture_to_csv(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "run.csv"
    code, stdout, _err = run(capsys, capture.main, *BENCH, *ASSERT, *FAST, "--out", str(out))
    assert code == 0
    with out.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file, quoting=csv.QUOTE_NOTNULL))
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["format"] == capture.CAPTURE_FORMAT
    assert document["state"] == "finished"
    assert document["error"] is None
    assert document["device"] == "zpa"
    assert document["channels"] == ["CH1", "CH2", "CH3"]
    assert document["analyzer"]["serial_number"] == "N8A0259T"
    assert document["metadata"]["response_time_o2_s"] == 15
    assert document["metadata"]["clock"] is not None
    assert document["arguments"]["gas"] == {"CH1": "co2", "CH2": "co", "CH3": "o2"}
    assert document["versions"]["fujilib"]
    summary = document["summary"]
    assert len(rows) == summary["polls"] > 0
    assert summary["failed_polls"] == 0
    assert summary["traffic"]["requests"] > 0
    assert f"data: {out}" in stdout


def test_capture_to_parquet(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "run.parquet"
    code, _out, _err = run(capsys, capture.main, *BENCH, *ASSERT, *FAST, "--out", str(out))
    assert code == 0
    rows = read_parquet(out)
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert len(rows) == document["summary"]["polls"]
    start = json.loads(parquet_metadata(out)["fujilib.capture"])
    assert start["state"] == "recording"
    assert start["analyzer"]["serial_number"] == "N8A0259T"


def test_capture_into_a_new_directory(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "new" / "deeper" / "run.csv"
    code, stdout, _err = run(capsys, capture.main, *BENCH, *FAST, "--out", str(out), "--quiet")
    assert code == 0
    assert out.exists()
    assert "traffic:" in stdout


def test_capture_when_its_directory_cannot_be_made(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    out = blocker / "run.csv"  # its "directory" is a file
    code, _out, err = run(capsys, capture.main, *BENCH, *FAST, "--out", str(out))
    assert code == 1
    assert "cannot create the directory" in err


def test_capture_format_from_the_argument(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "run.data"
    argv = [*BENCH, *ASSERT, *FAST, "--out", str(out), "--format", "csv", "--quiet"]
    code, _out, _err = run(capsys, capture.main, *argv)
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith('"device"')


def test_capture_does_not_replace_files_without_force(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "run.csv"
    argv = [*BENCH, *ASSERT, *FAST, "--out", str(out)]
    assert capture.main(argv) == 0
    out.unlink()  # the .meta.json alone still blocks
    with pytest.raises(SystemExit) as info:
        _ = capture.main(argv)
    assert info.value.code == 2
    assert "give --force" in capsys.readouterr().err
    assert capture.main([*argv, "--force"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        [*BENCH, "--out", "run.txt"],
        [*BENCH],
        ["--out", "x.csv"],
    ],
)
def test_capture_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _ = capture.main(argv)
    assert info.value.code == 2
    _ = capsys.readouterr()


def test_capture_to_parquet_without_pyarrow(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    out = tmp_path / "run.parquet"
    code, _out, err = run(capsys, capture.main, *BENCH, *FAST, "--out", str(out))
    assert code == 1
    assert "fujilib[parquet]" in err
    assert not capture.metadata_path(out).exists()  # nothing was opened


def test_capture_stops_cleanly_on_ctrl_c(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "PollSourceAdapter", Interrupting)
    out = tmp_path / "run.parquet"
    code, stdout, err = run(
        capsys, capture.main, *BENCH, *ASSERT, "--rate", "20", "--out", str(out)
    )
    assert code == 0
    assert "stopped by Ctrl-C" in err
    assert "polls:" in stdout
    assert "metadata:" in stdout
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["state"] == "stopped"
    assert len(read_parquet(out)) == document["summary"]["polls"] >= 2


def test_capture_stopped_before_it_records(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "device_name", interrupt_now)
    code, stdout, err = run(capsys, capture.main, *BENCH, "--out", str(tmp_path / "a.csv"))
    assert code == 0
    assert "stopped by Ctrl-C" in err
    assert stdout == ""


def test_capture_when_the_file_cannot_be_opened(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "run.csv"
    out.mkdir()
    code, _out, err = run(capsys, capture.main, *BENCH, *FAST, "--out", str(out), "--force")
    assert code == 1
    assert "cannot open" in err
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["state"] == "failed"
    assert document["summary"] is None


def test_capture_ends_with_a_connection_failure(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "PollSourceAdapter", Unplugging)
    out = tmp_path / "run.csv"
    code, _out, err = run(capsys, capture.main, *BENCH, *ASSERT, "--rate", "20", "--out", str(out))
    assert code == 1
    assert "error: unplugged" in err
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["state"] == "failed"
    assert document["error"] == "FujiConnectionError: unplugged"
    assert (document["summary"]["disconnects"], document["summary"]["reconnects"]) == (1, 0)
    with out.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file, quoting=csv.QUOTE_NOTNULL))
    assert rows[-1]["error_type"] == "fujilib.errors.FujiConnectionError"


def test_capture_prints_progress(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "_PROGRESS_S", 0.05)
    argv = [*BENCH, *ASSERT, "--rate", "20", "--duration", "0.3", "--out", str(tmp_path / "a.csv")]
    code, _out, err = run(capsys, capture.main, *argv)
    assert code == 0
    assert " polls, " in err


def test_capture_when_the_metadata_cannot_be_written(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    async def refuse(self: object, *args: object, **kwargs: object) -> int:
        raise OSError("read-only")

    monkeypatch.setattr(anyio.Path, "write_text", refuse)
    code, _out, err = run(capsys, capture.main, *BENCH, *FAST, "--out", str(tmp_path / "a.csv"))
    assert code == 1
    assert "read-only" in err


def test_capture_records_missing_package_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    def version(name: str) -> str:
        if name == "pyarrow":
            raise PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr("fujilib.cli.capture.version", version)
    found = capture._versions()
    assert found["pyarrow"] is None
    assert found["fujilib"]


def test_capture_writes_its_counters_while_it_records(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = capture._write_json
    written: list[dict[str, Any]] = []

    async def keep(path: Path, document: dict[str, object]) -> None:
        await real(path, document)
        written.append(json.loads(json.dumps(document, default=str)))

    monkeypatch.setattr(capture, "_CHECKPOINT_S", 0.05)
    monkeypatch.setattr(capture, "_write_json", keep)
    out = tmp_path / "run.parquet"
    argv = [*BENCH, *ASSERT, "--rate", "20", "--duration", "0.5", "--out", str(out), "--quiet"]
    code, _out, _err = run(capsys, capture.main, *argv)
    assert code == 0
    checkpoints = [d for d in written if d["state"] == "recording" and d["summary"] is not None]
    assert checkpoints
    assert all(d["finished_at"] is None for d in checkpoints)
    assert checkpoints[-1]["summary"]["polls"] > 0
    stamps = [d["updated_at"] for d in written]
    assert stamps == sorted(stamps)
    assert written[-1]["state"] == "finished"
    assert not (tmp_path / "run.parquet.meta.json.part").exists()


def test_capture_goes_on_when_a_checkpoint_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = capture._write_json

    async def busy(path: Path, document: dict[str, object]) -> None:
        if document["state"] == "recording" and document["summary"] is not None:
            msg = f"cannot write {path}: busy"
            raise FujiConfigurationError(msg)
        await real(path, document)

    monkeypatch.setattr(capture, "_CHECKPOINT_S", 0.02)
    monkeypatch.setattr(capture, "_write_json", busy)
    out = tmp_path / "run.csv"
    argv = [*BENCH, *ASSERT, "--rate", "20", "--duration", "0.3", "--out", str(out), "--quiet"]
    code, _out, err = run(capsys, capture.main, *argv)
    assert code == 0
    assert err.count("warning: cannot write") == 1
    assert "busy; recording goes on" in err
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["state"] == "finished"


def test_a_console_that_stops_taking_output_does_not_hold_up_the_recording(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    class Selected(io.StringIO):
        """A console window with a selection: every write waits."""

        def write(self, s: str) -> int:
            _ = release.wait(10)
            return len(s)

    monkeypatch.setattr(capture, "_PROGRESS_S", 0.02)
    monkeypatch.setattr(sys, "stderr", Selected())
    out = tmp_path / "run.csv"
    started = time.monotonic()
    try:
        code, _out, _err = run(
            capsys,
            capture.main,
            *BENCH,
            *ASSERT,
            "--rate",
            "20",
            "--duration",
            "0.5",
            "--out",
            str(out),
        )
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert code == 0
    summary = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))["summary"]
    assert summary["polls"] + summary["late"] == summary["target_polls"]
    assert summary["polls"] > summary["late"]


# --- Ctrl-Break -------------------------------------------------------------------------------

# Windows' Ctrl-Break; elsewhere a stand-in that is otherwise unused in these tests.
if sys.platform == "win32":
    BREAK = signal.SIGBREAK
else:
    BREAK = signal.SIGUSR1


class Breaking(Interrupting):
    """Sends this process Ctrl-Break (or its stand-in) during poll ``at``."""

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        self.polls += 1
        if self.polls == self.at:
            signal.raise_signal(BREAK)
        return await PollSourceAdapter.poll(self, names)


def test_capture_stops_cleanly_on_ctrl_break(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_recording, "_CTRL_BREAK", BREAK)
    monkeypatch.setattr(capture, "PollSourceAdapter", Breaking)
    before = signal.getsignal(BREAK)
    out = tmp_path / "run.parquet"
    code, stdout, err = run(
        capsys, capture.main, *BENCH, *ASSERT, "--rate", "20", "--out", str(out)
    )
    assert code == 0
    assert "stopped by Ctrl-C" in err
    assert "polls:" in stdout
    document = json.loads(capture.metadata_path(out).read_text(encoding="utf-8"))
    assert document["state"] == "stopped"
    assert len(read_parquet(out)) == document["summary"]["polls"] >= 2
    assert signal.getsignal(BREAK) is before


def test_ctrl_break_is_left_alone_where_there_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_recording, "_CTRL_BREAK", None)
    before = signal.getsignal(BREAK)
    with _recording.ctrl_break_as_ctrl_c():
        assert signal.getsignal(BREAK) is before


def test_ctrl_break_is_left_alone_when_ctrl_c_has_no_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_recording, "_CTRL_BREAK", BREAK)
    before = signal.getsignal(BREAK)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with _recording.ctrl_break_as_ctrl_c():
            assert signal.getsignal(BREAK) is before
    finally:
        _ = signal.signal(signal.SIGINT, previous)


def test_ctrl_break_calls_the_ctrl_c_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_recording, "_CTRL_BREAK", BREAK)
    seen: list[int] = []
    previous = signal.signal(signal.SIGINT, lambda signum, frame: seen.append(signum))
    try:
        with _recording.ctrl_break_as_ctrl_c():
            signal.raise_signal(BREAK)
    finally:
        _ = signal.signal(signal.SIGINT, previous)
    assert seen == [signal.SIGINT]


# --- fuji-diag timing -------------------------------------------------------------------------


def test_timing_on_the_simulator(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "timing.json"
    argv = ["timing", *BENCH, "--trials", "2", "--gaps-ms", "0,1", "--seed", "7", "--out", str(out)]
    code, stdout, _err = run(capsys, diag.main, *argv)
    assert code == 0
    assert stdout.startswith("first      second")
    assert "16 trials written" in stdout
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["format"] == diag.TIMING_FORMAT
    assert document["arguments"]["seed"] == 7
    trials = document["trials"]
    assert len(trials) == 16
    assert not any(t["failed"] for t in trials)
    assert {(t["first"], t["second"]) for t in trials} == {
        ("normal", "normal"),
        ("normal", "exception"),
        ("exception", "normal"),
        ("exception", "exception"),
    }
    assert all(t["gap_ms"] >= t["target_gap_ms"] for t in trials)
    assert len(document["summary"]) == 8


def test_timing_without_a_file(capsys: pytest.CaptureFixture[str]) -> None:
    code, stdout, _err = run(capsys, diag.main, "timing", *BENCH, "--trials", "1")
    assert code == 0
    assert "trials written" not in stdout


def test_timing_on_a_port_that_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def open_dead(settings: SerialSettings) -> SerialTransport:
        config = SerialConfig(baudrate=38_400)
        host, device = serial_port_pair(config_a=config, config_b=config)
        await device.aclose()
        return SerialTransport(host, settings)

    monkeypatch.setattr(SerialTransport, "open", open_dead)
    out = tmp_path / "t.json"
    code, _out, err = run(capsys, diag.main, "timing", "COM8", "--trials", "1", "--out", str(out))
    assert code == 1
    assert "error:" in err
    document = json.loads(out.read_text(encoding="utf-8"))  # what was measured is kept
    assert document["completed"] is False
    assert "FujiConnectionError" in document["error"]


def test_timing_errors(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _out, err = run(capsys, diag.main, "timing", "--fixture", str(tmp_path / "none.json"))
    assert (code, "cannot read the register bank" in err) == (1, True)
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    out = blocker / "t.json"  # its "directory" is a file
    code, _out, err = run(capsys, diag.main, "timing", *BENCH, "--trials", "1", "--out", str(out))
    assert (code, "cannot create the directory" in err) == (1, True)


def test_timing_when_its_file_cannot_be_written(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    async def refuse(self: object, *args: object, **kwargs: object) -> int:
        raise OSError("read-only")

    monkeypatch.setattr(anyio.Path, "write_text", refuse)
    out = tmp_path / "t.json"
    code, _out, err = run(capsys, diag.main, "timing", *BENCH, "--trials", "1", "--out", str(out))
    assert code == 1
    assert "cannot write" in err


def test_timing_creates_the_directory_and_needs_force(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "new" / "t.json"
    argv = ["timing", *BENCH, "--trials", "1", "--out", str(out)]
    assert diag.main(argv) == 0
    with pytest.raises(SystemExit) as info:
        _ = diag.main(argv)
    assert info.value.code == 2
    assert diag.main([*argv, "--force"]) == 0
    _ = capsys.readouterr()


def test_timing_keeps_what_it_measured_when_stopped(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = diag._trial
    count = 0

    async def interrupting(*args: Any) -> dict[str, object]:
        nonlocal count
        count += 1
        if count == 3:
            signal.raise_signal(signal.SIGINT)
        return await real(*args)

    monkeypatch.setattr(diag, "_trial", interrupting)
    out = tmp_path / "t.json"
    code, stdout, err = run(capsys, diag.main, "timing", *BENCH, "--trials", "5", "--out", str(out))
    assert code == 0
    assert "stopped by Ctrl-C after" in err
    assert stdout.startswith("first")
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["completed"] is False
    assert 2 <= len(document["trials"]) < 80


async def _one_trial(fault: FaultKind | None) -> dict[str, object]:
    async with mock_analyzer_pair(
        read_retries=0, request_timeout=0.05, inter_frame_idle=0.0, resync_window=0.01
    ) as (client, mock):
        if fault is not None:
            mock.inject(fault)
        return await diag._trial(client, "normal", "exception", 1.0)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_trial_whose_first_read_failed_is_not_judged() -> None:
    good = await _one_trial(None)
    assert (good["judged"], good["failed"], good["second_outcome"]) == (True, False, "exception 02")
    bad = await _one_trial(FaultKind.DROP)
    assert (bad["first_outcome"], bad["judged"], bad["failed"]) == ("timeout", False, False)


@pytest.mark.parametrize(
    "argv",
    [
        ["timing"],
        ["timing", "COM8", *BENCH],
        ["timing", *BENCH, "--gaps-ms", "a,b"],
        ["timing", *BENCH, "--gaps-ms", "5000"],
        ["timing", *BENCH, "--trials", "0"],
        ["timing", *BENCH, "--trials", "x"],
        [],
    ],
)
def test_timing_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _ = diag.main(argv)
    assert info.value.code == 2
    _ = capsys.readouterr()


def test_outcomes() -> None:
    coded = FujiModbusError("refused", context=ErrorContext(extra={"exception_code": 4}))
    assert [
        diag._outcome(e)
        for e in (
            None,
            FujiModbusIllegalDataAddressError("x"),
            FujiModbusTimeoutError("x"),
            coded,
            FujiModbusError("x"),
            FujiFrameError("x"),
            FujiError("x"),
        )
    ] == ["ok", "exception 02", "timeout", "exception 04", "exception", "damaged", "FujiError"]


def test_summaries_count_failures() -> None:
    trial: dict[str, object] = {
        "first": "normal",
        "second": "normal",
        "target_gap_ms": 0.0,
        "gap_ms": 0.1,
        "first_outcome": "ok",
        "second_outcome": "timeout",
        "judged": True,
        "failed": True,
    }
    unjudged = {**trial, "first_outcome": "timeout", "judged": False, "failed": False}
    ok = {**trial, "failed": False, "second_outcome": "ok"}
    (row,) = diag.summarize([trial, ok, unjudged])
    assert (row["trials"], row["failed"], row["first_bad"]) == (3, 1, 1)
    assert row["failures"] == {"timeout": 1}
    assert "{'timeout': 1}" in diag._table([row])
