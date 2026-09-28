"""``fuji-decode``: frames, fixtures and dumps, in-process (design §7.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from anymodbus.crc import crc16_modbus_bytes

from fujilib.cli._common import render
from fujilib.cli.decode import describe_dump, describe_frame, main
from fujilib.devices.capability import Capability
from fujilib.errors import FujiDecodeError
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.units import Unit
from fujilib.testing import BENCH_BANK_PATH

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANUAL_FRAMES = FIXTURES / "manual_frames.txt"


def adu(*body: int) -> bytes:
    frame = bytes(body)
    return frame + crc16_modbus_bytes(frame)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- Frames --------------------------------------------------------------------------------


def test_fixture_text(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--fixture", str(MANUAL_FRAMES))
    assert code == 0
    assert "12.00 vol%" in out
    assert "key simulation: ZERO" in out
    assert "outside the write envelope: fujilib never sends this" in out
    assert "calibration_gas.ch2.range1.span" in out


def test_fixture_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--fixture", str(MANUAL_FRAMES), "--format", "json")
    assert code == 0
    report = json.loads(out)
    assert len(report) == 5
    assert report[0]["reply"] is None
    fc10 = report[4]
    assert fc10["request"]["values"] == [5000, 10, 1000, 10]
    assert fc10["request"]["write_envelope"] == "inside the write envelope"
    assert fc10["reply"]["kind"] == "reply"


def test_hex_reply_with_start(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys, "--hex", "01 04 06 04 B0 00 02 00 00 81 0D", "--start", "0x000C", "--format", "json"
    )
    assert code == 0
    report = json.loads(out)
    assert report["crc"] == "valid"
    assert report["registers"][0] == {
        "address": "000Ch (30013)",
        "name": "reading.ch5.value",
        "raw": 1200,
        "value": "12.00 vol%",
    }


def test_hex_reply_without_start_lists_words_only() -> None:
    report = describe_frame(bytes.fromhex("0104060 4B0000200008 10D".replace(" ", "")))
    assert report["words"] == [1200, 2, 0]
    assert "registers" not in report


@pytest.mark.parametrize(
    ("frame", "key", "expected"),
    [
        (adu(1, 0x84, 0x02), "exception", "02 illegal data address"),
        (adu(1, 0x83, 0x03), "exception", "03 illegal data value"),
        (adu(1, 0x81, 0x09), "exception", "09 unknown"),
        (adu(1, 0x06, 0x07, 0xD1, 0, 1), "meaning", "return_to_measurement"),
        (adu(1, 0x06, 0x00, 0x48, 0, 1), "meaning", "unmapped"),
        (adu(1, 0x06, 0x00, 0x49, 0, 1), "meaning", "key_lock"),
        (adu(1, 0x10, 0x00, 0x23, 0, 4), "kind", "reply"),
        (adu(1, 0x08, 0, 0, 0x12, 0x34), "function", "08 not used by the ZP series"),
    ],
)
def test_frame_kinds(frame: bytes, key: str, expected: str) -> None:
    assert str(describe_frame(frame)[key]).startswith(expected)


def test_bad_crc_is_reported() -> None:
    frame = bytearray(adu(1, 0x04, 0, 0x0C, 0, 3))
    frame[-1] ^= 0xFF
    assert describe_frame(bytes(frame))["crc"] == "INVALID"


def test_register_values_in_replies() -> None:
    holding = describe_frame(adu(1, 0x03, 4, 0, 7, 0, 1), start=0x45)
    registers: list[dict[str, Any]] = holding["registers"]  # type: ignore[assignment]
    assert registers[0] == {"address": "0045h (40070)", "name": "auto_calibration.cycle", "raw": 7}
    assert registers[1]["value"] == "days"
    gas = describe_frame(adu(1, 0x03, 2, 0x07, 0xD0), start=0x01)
    assert gas["registers"][0]["value"] == "needs the range's decimal point"  # type: ignore[index]
    bad_chars = describe_frame(adu(1, 0x04, 52, 0x41, 0x00, *([0, 0x41] * 25)), start=0x448)
    assert "error" in bad_chars["registers"][0]  # type: ignore[index]


def test_bad_hex_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "--hex", "01 0G")
    assert code == 2
    assert "--hex" in err


def test_short_frame_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "--hex", "01 04 00")
    assert code == 1
    assert "too short" in err
    with pytest.raises(FujiDecodeError):
        describe_frame(b"\x01\x04")


# --- Dumps ------------------------------------------------------------------------------------


def test_bench_dump(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys,
        "--dump",
        str(BENCH_BANK_PATH),
        "--gas",
        "CH1=co2",
        "--gas",
        "ch2=CO",
        "--gas",
        "CH3=o2",
        "--format",
        "json",
    )
    assert code == 0
    report = json.loads(out)
    assert report["identity"]["serial_number"] == "N8A0259T"
    assert report["identity"]["unknown_digits"] == [4, 25, 26]
    assert [c["label_source"] for c in report["channels"]] == ["asserted"] * 3
    assert report["readings"]["channels"][2]["value"] == "20.18 vol%"
    assert report["readings"]["analyzer"]["display"] == "measurement"
    assert len(report["error_log"]) == 14
    assert report["error_log"][0] == {
        "error": "6 span_out_of_range",
        "channel": "CH1",
        "at": "day 6 15:49",
    }
    assert report["clock"] == "2026-09-28 14:46:12"
    assert report["adc"]["reference_voltage"] == 38_928
    assert report["settings"]["response_time_s"] == {"CH1": 15, "CH2": 15, "CH3": 15}
    assert report["settings"]["calibration_gas"]["CH3 range 1"] == [0.0, 20.95]


def test_bench_dump_text(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--dump", str(BENCH_BANK_PATH))
    assert code == 0
    assert "serial_number: N8A0259T" in out
    assert "label_source: inferred" in out  # CH3 O2 without an assertion
    assert "errors: none" in out


def test_partial_dump_says_what_is_missing() -> None:
    report = describe_dump({"holding": {}, "input": {}, "captured_utc": "not a time"})
    assert str(report["identity"]).startswith("not decodable from this dump")
    assert str(report["channels"]).startswith("not decodable from this dump")
    assert str(report["clock"]).startswith("not decodable from this dump")
    assert str(report["settings"]).startswith("not decodable from this dump")


def test_dump_asserted_labels() -> None:
    data = json.loads(BENCH_BANK_PATH.read_text(encoding="utf-8"))
    report = describe_dump(data, asserted={ChannelId.CH4: Gas.CH4})
    channels: list[dict[str, Any]] = report["channels"]  # type: ignore[assignment]
    assert channels[-1]["channel"] == "CH4"
    assert channels[-1]["gas"] == "ch4"


def test_malformed_dump_is_a_validation_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = tmp_path / "bad.json"
    dump.write_text(json.dumps({"input": [1, 2]}), encoding="utf-8")
    code, _, err = run(capsys, "--dump", str(dump))
    assert code == 1
    assert "not an address" in err


@pytest.mark.parametrize("gas", ["CH3", "CH3=argon", "CH13=o2"])
def test_bad_gas_argument(gas: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--dump", str(BENCH_BANK_PATH), "--gas", gas])
    assert info.value.code == 2
    assert "--gas" in capsys.readouterr().err


def test_reading_with_a_bad_decimal_point_is_left_raw() -> None:
    report = describe_frame(adu(1, 0x04, 6, 0x04, 0xB0, 0, 9, 0, 0), start=0x0C)
    registers: list[dict[str, Any]] = report["registers"]  # type: ignore[assignment]
    assert registers[0] == {"address": "000Ch (30013)", "name": "reading.ch5.value", "raw": 1200}


def test_dump_keeps_undocumented_values() -> None:
    data = json.loads(BENCH_BANK_PATH.read_text(encoding="utf-8"))
    data["holding"]["008B"] = 7  # hold mode, documented 0-1
    report = describe_dump(data)
    assert report["settings"]["hold_mode"] == 7  # type: ignore[index]


def test_render() -> None:
    assert render("plain", "text") == "plain\n"
    assert render({"unit": Unit.PPM, "empty": [], "flag": False}, "text") == (
        "unit: ppm\nempty: none\nflag: no\n"
    )
    report = {"unit": Unit.PPM, "frame": b"\x01\x02", "at": Path("x"), "cap": Capability.CLOCK}
    payload = json.loads(render(report, "json"))
    assert payload == {"unit": "ppm", "frame": "01 02", "at": "x", "cap": 1}


def test_dump_capture_time_per_table() -> None:
    data = json.loads(BENCH_BANK_PATH.read_text(encoding="utf-8"))
    data["captured_utc"] = {"input": "2026-09-28T15:43:50+00:00", "holding": "x"}
    report = describe_dump(data)
    assert report["clock"] == "2026-09-28 14:46:12"
