"""``fuji-read``, ``fuji-discover`` and ``fuji-configure``, run in-process (design §7.7).

``fuji-read`` and ``fuji-configure`` run against the bundled bench bank with
``--fixture bench``, through ``open_device`` and a simulated analyzer.
``fuji-discover`` scans real ports by name, so its tests stand in for
discovery and check what the command makes of the results.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from fujilib import (
    CalibrationLogEntry,
    ChannelId,
    DiscoveryResult,
    FujiConnectionError,
    FujiModbusTimeoutError,
    Gas,
    PartialTimestamp,
    ProtocolKind,
)
from fujilib.cli import _common, configure, discover, read
from fujilib.cli._report import calibration_log_report
from fujilib.cli.configure import SETTINGS_FORMAT
from fujilib.cli.discover import parse_addresses
from fujilib.devices.analyzer import Analyzer
from fujilib.devices.decode import (
    decode_current_ranges,
    decode_identity,
    decode_ranges,
    nonzero_channels,
)
from fujilib.devices.profile import ZP_PROFILE
from fujilib.devices.reads import Identity
from fujilib.devices.session import Session, describe_identity
from fujilib.errors import FujiVerificationError
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.registry.enums import CalibrationKind
from fujilib.testing import BENCH_BANK_PATH
from fujilib.transport.base import SerialSettings
from fujilib.transport.serial import SerialTransport
from tests.factories import bench_banks

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fujilib.devices.models import DeviceInfo
    from fujilib.devices.settings import ApplyReport

BENCH = ["--fixture", "bench"]
ASSERT = ["--gas", "CH1=co2", "--gas", "CH2=co", "--gas", "CH3=o2"]


def run(capsys: pytest.CaptureFixture[str], main: Callable[[list[str]], int], *argv: str) -> Any:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- fuji-read -------------------------------------------------------------------------------


def test_read_identity_and_a_poll(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, read.main, *BENCH, *ASSERT, "--format", "json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert list(report) == ["identity", "poll"]
    assert report["identity"]["serial_number"] == "N8A0259T"
    assert report["identity"]["channels"][2]["label_source"] == "asserted"
    assert report["poll"]["channels"][2] == {
        "channel": "CH3",
        "gas": "o2",
        "value": "20.18 vol%",
        "raw": 2018,
        "state": "ok",
    }


def test_read_everything(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys, read.main, *BENCH, "--all", "--format", "json")
    assert code == 0
    report = json.loads(out)
    assert list(report) == list(read.SECTIONS)
    assert report["metadata"]["response_time_o2_s"] == 15
    assert report["metadata"]["current_range"]["CH3"] == 1
    assert report["metadata"]["clock"].startswith("2026-")
    assert report["clock"]["clock"].startswith("2026-")
    assert len(report["error-log"]) == 14
    assert report["calibration-log"].startswith("not available: ")
    assert report["adc"]["reference_voltage"] == 38_928
    assert report["snapshot"]["model"] == "ZPA"
    assert report["ranges"][2]["ranges"] == ["0-21.00 vol%", "0-25.00 vol%"]
    assert report["status"]["display"] == "measurement"


def test_read_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys, read.main, *BENCH, "--include", "poll")
    assert code == 0
    assert out.startswith("poll:\n  channels:\n")
    assert "value: 20.18 vol%" in out


def test_read_another_station_of_the_bank(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(
        capsys, read.main, *BENCH, "--address", "7", "--timeout", "0.4", "--format", "json"
    )
    assert code == 0
    assert json.loads(out)["identity"]["address"] == 7


def test_read_a_bank_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bank = tmp_path / "bank.json"
    bank.write_text(BENCH_BANK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    code, out, _err = run(capsys, read.main, "--fixture", str(bank), "--include", "snapshot")
    assert code == 0
    assert "model: ZPA" in out


def test_read_a_missing_bank(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, err = run(capsys, read.main, "--fixture", str(tmp_path / "missing.json"))
    assert (code, out) == (1, "")
    assert err.startswith("error: cannot read the register bank")


def test_read_a_port_that_does_not_exist(capsys: pytest.CaptureFixture[str]) -> None:
    name = "COM249" if sys.platform == "win32" else "/dev/fujilib-no-such-port"
    code, _out, err = run(capsys, read.main, name)
    assert code == 1
    assert err.startswith("error: cannot open")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["COM8", "--fixture", "bench"],
        ["--fixture", "bench", "--gas", "CH3"],
        ["--fixture", "bench", "--gas", "CH3=unknown"],
        ["--fixture", "bench", "--gas", "CH3=o2", "--gas", "ch3=co"],
        ["--fixture", "bench", "--address", "40"],
        ["--fixture", "bench", "--address", "x"],
        ["--fixture", "bench", "--timeout", "0"],
        ["--fixture", "bench", "--timeout", "nan"],
        ["--fixture", "bench", "--timeout", "x"],
    ],
)
def test_read_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        read.main(argv)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err


# --- fuji-configure --------------------------------------------------------------------------


def test_dump_the_settings(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys, configure.main, "dump", *BENCH, "--alarm-target", "1=CH1")
    assert code == 0
    document = json.loads(out)
    assert document["format"] == SETTINGS_FORMAT
    assert document["analyzer"]["serial_number"] == "N8A0259T"
    settings = document["settings"]
    assert settings["response_time.o2"] == {
        "value": 15,
        "raw": 15,
        "unit": "s",
        "access": "read_write",
        "safety": "persistent",
        "evidence": "documented",
    }
    assert settings["hold.mode"]["value"] == "last_value"
    assert isinstance(settings["alarm1.range1.high"]["value"], float)
    assert settings["alarm2.range1.high"]["value"] is None
    assert all("0x" not in name and not name[0].isdigit() for name in settings)


def test_dump_to_a_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out_file = tmp_path / "settings.json"
    code, out, _err = run(capsys, configure.main, "dump", *BENCH, "--out", str(out_file))
    document = json.loads(out_file.read_text(encoding="utf-8"))
    assert code == 0
    assert out == f"wrote {len(document['settings'])} settings to {out_file}\n"


def test_dump_to_a_file_that_cannot_be_written(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out_file = tmp_path / "missing" / "settings.json"
    code, out, err = run(capsys, configure.main, "dump", *BENCH, "--out", str(out_file))
    assert (code, out) == (1, "")
    assert err.startswith("error: cannot write")


def test_dump_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys, configure.main, "dump", *BENCH, "--format", "text")
    assert code == 0
    assert "format: fujilib-settings/1" in out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["dump"],
        ["dump", *BENCH, "--alarm-target", "1"],
        ["dump", *BENCH, "--alarm-target", "x=CH1"],
        ["dump", *BENCH, "--alarm-target", "7=CH1"],
    ],
)
def test_configure_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        configure.main(argv)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err


def settings_file(tmp_path: Path, settings: dict[str, Any], **analyzer: str) -> str:
    path = tmp_path / "settings.json"
    document = {"format": SETTINGS_FORMAT, "analyzer": analyzer, "settings": settings}
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


CHANGES: dict[str, Any] = {
    "response_time.o2": 16,
    "calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"},
}


def test_diff_says_what_would_change(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    doc = settings_file(tmp_path, {**CHANGES, "key_lock": True, "hold.mode": "last_value"})
    code, out, _err = run(capsys, configure.main, "diff", *BENCH, "--file", doc, "--format", "json")
    assert code == 0
    report = json.loads(out)
    assert list(report["write"]) == ["response_time.o2", "calibration_gas.ch3.range1.span"]
    assert report["write"]["response_time.o2"] == {
        "action": "write",
        "current": "15 s",
        "wanted": "16",
        "safety": "persistent",
    }
    assert report["refused"]["key_lock"]["reason"].startswith("read-only")
    assert report["unchanged"] == 1
    assert report["tier"] == "dangerous"


def test_diff_of_another_analyzers_document(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = settings_file(tmp_path, {"response_time.o2": 16}, serial_number="Q1234567")
    code, out, _err = run(capsys, configure.main, "diff", *BENCH, "--file", doc)
    assert code == 0
    assert "analyzer: the document describes the analyzer with serial number" in out
    code, out, _err = run(capsys, configure.main, "diff", *BENCH, "--file", doc, "--any-analyzer")
    assert "analyzer:" not in out


def test_apply_writes_and_reads_back(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    doc = settings_file(tmp_path, CHANGES)
    code, out, err = run(
        capsys,
        configure.main,
        "apply",
        *BENCH,
        "--file",
        doc,
        "--confirm",
        "--i-understand-this-is-destructive",
        "--format",
        "json",
    )
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert report["status"] == "ok"
    assert report["written"]["response_time.o2"] == {
        "before": "15 s",
        "written": "16 s",
        "read_back": "16 s",
        "state": "verified",
        "acknowledged": True,
    }
    assert "recovery" not in report


def test_apply_needs_confirm_before_anything_opens(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = settings_file(tmp_path, CHANGES)
    with pytest.raises(SystemExit) as caught:
        configure.main(["apply", *BENCH, "--file", doc])
    assert caught.value.code == 2
    assert "pass --confirm" in capsys.readouterr().err


def test_a_dangerous_apply_needs_the_destructive_flag(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = settings_file(tmp_path, CHANGES)
    code, out, err = run(capsys, configure.main, "apply", *BENCH, "--file", doc, "--confirm")
    assert code == 2
    assert "--i-understand-this-is-destructive" in err
    assert "calibration_gas.ch3.range1.span" in out
    code, _out, err = run(
        capsys,
        configure.main,
        "apply",
        *BENCH,
        "--file",
        settings_file(tmp_path, {"response_time.o2": 16}),
        "--confirm",
    )
    assert (code, err) == (0, "")


def test_apply_dry_run_and_nothing_to_do(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = settings_file(tmp_path, CHANGES)
    code, out, _err = run(capsys, configure.main, "apply", *BENCH, "--file", doc, "--dry-run")
    assert code == 0
    assert "status: dry_run" in out
    same = settings_file(tmp_path, {"response_time.o2": 15})
    code, out, _err = run(capsys, configure.main, "apply", *BENCH, "--file", same, "--confirm")
    assert code == 0
    assert out.rstrip().endswith("status: ok")


def test_a_refused_document_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = settings_file(tmp_path, {"response_time.o2": 16, "start_auto_calibration": 1})
    code, out, _err = run(capsys, configure.main, "apply", *BENCH, "--file", doc, "--confirm")
    assert code == 1
    assert "status: refused" in out
    assert "operation command" in out
    assert "recovery: nothing was written" in out


def test_an_apply_that_fails_says_what_to_do(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Analyzer.apply_settings

    async def failing(self: Analyzer, *args: Any, **kwargs: Any) -> ApplyReport:
        report = await real(self, *args, **kwargs)
        error = FujiVerificationError("response_time.o2: wrote 16 s, but it reads back as 15 s")
        return replace(report, completed=(), failed="response_time.o2", error=error)

    monkeypatch.setattr(Analyzer, "apply_settings", failing)
    doc = settings_file(tmp_path, {"response_time.o2": 16})
    code, out, _err = run(capsys, configure.main, "apply", *BENCH, "--file", doc, "--confirm")
    assert code == 1
    assert "status: verify_failed" in out
    assert "recovery: the setting reads back otherwise" in out
    assert "not_attempted" in out


@pytest.mark.parametrize(("flag", "ceiling"), [(False, "PERSISTENT"), (True, "DANGEROUS")])
def test_apply_checks_the_ceiling_on_its_own_comparison(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: bool,
    ceiling: str,
) -> None:
    seen: list[str] = []
    real = Analyzer.apply_settings

    async def recording(self: Analyzer, *args: Any, **kwargs: Any) -> ApplyReport:
        seen.append(kwargs["max_tier"].name)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(Analyzer, "apply_settings", recording)
    doc = settings_file(tmp_path, {"response_time.o2": 16})
    extra = ["--i-understand-this-is-destructive"] if flag else []
    code, _out, _err = run(
        capsys, configure.main, "apply", *BENCH, "--file", doc, "--confirm", *extra
    )
    assert code == 0
    assert seen == [ceiling]


@pytest.mark.parametrize(
    "content", ["not json", json.dumps({"format": "other", "settings": {}}), None]
)
def test_an_unreadable_document_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "doc.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        configure.main(["diff", *BENCH, "--file", str(path)])
    assert caught.value.code == 2
    assert "cannot read the settings document" in capsys.readouterr().err


# --- fuji-discover ---------------------------------------------------------------------------


def found(port: str, address: int, *, error: Exception | None = None) -> DiscoveryResult:
    return DiscoveryResult(
        ok=True,
        port=port,
        address=address,
        baudrate=38_400,
        protocol=ProtocolKind.MODBUS_RTU,
        device_info=None,
        error=error,  # type: ignore[arg-type]
        elapsed_s=0.2,
        model="ZPA",
    )


def silent(port: str, address: int) -> DiscoveryResult:
    return DiscoveryResult(
        ok=False,
        port=port,
        address=address,
        baudrate=38_400,
        protocol=None,
        device_info=None,
        error=FujiModbusTimeoutError("no reply"),
        elapsed_s=0.4,
    )


def canned(monkeypatch: pytest.MonkeyPatch, results: list[DiscoveryResult]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def find_devices(**kwargs: Any) -> list[DiscoveryResult]:
        calls.append(kwargs)
        return results

    monkeypatch.setattr(discover, "find_devices", find_devices)
    return calls


def test_discover_reports_each_station(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = canned(monkeypatch, [found("COM8", 1), silent("COM8", 2)])
    code, out, _err = run(capsys, discover.main, "COM8", "--addresses", "1-2", "--no-identify")
    assert code == 0
    assert calls == [
        {"ports": ["COM8"], "addresses": (1, 2), "per_probe_timeout_s": 0.3, "identify": False}
    ]
    assert out.splitlines() == [
        "COM8 station 1: ZPA",
        "COM8 station 2: - no reply",
        "1 analyzer(s) found on 1 port(s)",
    ]


def test_discover_as_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    identify_failed = FujiModbusTimeoutError("no reply")
    canned(monkeypatch, [found("COM8", 1, error=identify_failed), silent("COM9", 1)])
    code, out, _err = run(capsys, discover.main, "COM8", "COM9", "--format", "json")
    assert code == 0
    report = json.loads(out)
    assert report["results"][0]["model"] == "ZPA"
    assert report["results"][0]["error"] == "no reply"
    assert report["ports"] == [
        {"port": "COM8", "found": [1], "probed": 1, "error": None},
        {"port": "COM9", "found": [], "probed": 1, "error": "no reply"},
    ]


def bench_info() -> DeviceInfo:
    _holding, bank = bench_banks()
    type_code, serial = decode_identity(bank)
    identity = Identity(
        type_code=type_code,
        serial_number=serial,
        ranges=decode_ranges(bank),
        nonzero=nonzero_channels(bank),
        current_ranges=decode_current_ranges(bank),
        probes=MappingProxyType({}),
        timings=(),
    )
    return describe_identity(identity, address=1, serial_settings=SerialSettings(port="COM8"))


def test_discover_with_identification(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    identified = replace(found("COM8", 1), device_info=bench_info())
    canned(monkeypatch, [identified])
    code, out, _err = run(capsys, discover.main, "COM8")
    assert code == 0
    assert out.splitlines()[0] == "COM8 station 1: ZPA N8A0259T"
    code, out, _err = run(capsys, discover.main, "COM8", "--format", "json")
    assert json.loads(out)["results"][0]["device"]["serial_number"] == "N8A0259T"


def test_discover_when_identification_failed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    canned(monkeypatch, [found("COM8", 1, error=FujiModbusTimeoutError("no reply"))])
    _code, out, _err = run(capsys, discover.main, "COM8")
    assert out.splitlines()[0] == "COM8 station 1: ZPA (identify failed: no reply)"


def test_discover_all_ports(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = canned(monkeypatch, [silent("COM8", 1)])
    code, out, _err = run(capsys, discover.main, "--all-ports")
    assert code == discover.NOTHING_FOUND
    assert calls[0]["ports"] is None
    assert out.splitlines()[-1] == "0 analyzer(s) found on 1 port(s)"


def test_discover_a_library_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find_devices(**_kwargs: Any) -> list[DiscoveryResult]:
        raise FujiConnectionError("cannot list the host's serial ports")

    monkeypatch.setattr(discover, "find_devices", find_devices)
    code, _out, err = run(capsys, discover.main, "--all-ports")
    assert code == 1
    assert err == "error: cannot list the host's serial ports\n"


@pytest.mark.parametrize(
    "argv",
    [[], ["COM8", "--all-ports"], ["COM8", "--addresses", "0"], ["COM8", "--probe-timeout", "0"]],
)
def test_discover_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        discover.main(argv)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "stations"),
    [("1", (1,)), ("1,2,5", (1, 2, 5)), ("1-3,7", (1, 2, 3, 7)), (" 2 , 2-3", (2, 3))],
)
def test_parse_addresses(text: str, stations: tuple[int, ...]) -> None:
    assert parse_addresses(text) == stations


@pytest.mark.parametrize("text", ["", "0", "32", "3-1", "1-", "a", "1-40"])
def test_parse_addresses_refuses(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_addresses(text)


def test_read_by_port_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []

    async def open_by_name(port: str, **kwargs: Any) -> Analyzer:
        # A port pair with nothing on the far end: only I/O-free sections work.
        opened.append((port, kwargs))
        config = SerialConfig(baudrate=38_400)
        host, device = serial_port_pair(config_a=config, config_b=config)
        await device.aclose()
        transport = SerialTransport(host, SerialSettings(port=port))
        return Analyzer(
            Session(ModbusPort(transport, owns_transport=True), address=1, profile=ZP_PROFILE)
        )

    monkeypatch.setattr(_common, "open_device", open_by_name)
    code, out, _err = run(capsys, read.main, "COM8", "--include", "snapshot", "--gas", "CH3=o2")
    assert code == 0
    assert "connected: yes" in out
    assert opened == [
        (
            "COM8",
            {
                "address": 1,
                "timeout": 0.5,
                "identify": True,
                "channel_map": {ChannelId.CH3: Gas.O2},
            },
        )
    ]


def test_the_calibration_log_report() -> None:
    entry = CalibrationLogEntry(
        channel=ChannelId.CH2,
        range=1,
        kind=CalibrationKind.SPAN_RANGE1,
        detector_count=100_000,
        deviation_percent_fs=1.5,
        at=PartialTimestamp(month=9, day=28, hour=14, minute=30),
    )
    assert calibration_log_report([entry]) == [
        {
            "channel": "CH2",
            "kind": "span_range1",
            "detector_count": 100_000,
            "deviation_percent_fs": 1.5,
            "at": "month 9 day 28 14:30",
        }
    ]
