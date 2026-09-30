"""``fuji-decode`` — decode MODBUS frames or a register dump offline, with no hardware.

Three inputs:

- ``--hex``: one frame. A read reply carries no register address, so give the
  block's first address with ``--start`` to have its words named and scaled.
- ``--fixture``: an arrow-format file; each reply is decoded against its request.
- ``--dump``: a register bank as JSON (``{"input": {"0000": 65525, ...},
  "holding": {...}}``, as the bench capture is), decoded into identity, ranges,
  readings, status, the error log, settings, the clock and the A/D values.
  ``--gas CH3=o2`` asserts a channel's gas.

Examples::

    fuji-decode --hex "01 04 06 04 B0 00 02 00 00 81 0D" --start 0x000C
    fuji-decode --fixture tests/fixtures/manual_frames.txt
    fuji-decode --dump src/fujilib/testing/zpa_bench_documented.json --gas CH1=co2 --gas CH3=o2
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from anymodbus.crc import verify_crc

from fujilib.cli._common import parse_gas, render, run_cli
from fujilib.cli._report import (
    adc_report,
    channels_report,
    error_log_report,
    frame_report,
    metadata_report,
    ranges_report,
    show,
    type_code_report,
)
from fujilib.devices.decode import (
    decode_adc,
    decode_clock,
    decode_current_ranges,
    decode_error_log,
    decode_frame,
    decode_identity,
    decode_metadata,
    decode_ranges,
    decode_register,
    label_channels,
    nonzero_channels,
    words_of,
)
from fujilib.devices.models import TransferTiming
from fujilib.errors import ErrorContext, FujiDecodeError, FujiValidationError
from fujilib.protocol.modbus.codec import decode_int, scale
from fujilib.registry.enums import KeyCode
from fujilib.registry.regions import (
    FC_READ_HOLDING,
    FC_READ_INPUT,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    RegisterTable,
)
from fujilib.registry.registers import REGISTRY
from fujilib.registry.units import unit_from_code
from fujilib.registry.write_policy import KEY_SIMULATION_ADDRESS, OPERATIONS, envelope_allows
from fujilib.testing import hex_to_bytes, parse_arrow_fixture

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fujilib.devices.decode import Bank
    from fujilib.devices.models import ChannelInfo
    from fujilib.registry.channels import ChannelId, Gas

__all__ = ["describe_dump", "describe_frame", "main"]

_FUNCTIONS: Final = {
    FC_READ_HOLDING: "read holding registers",
    FC_READ_INPUT: "read input registers",
    FC_WRITE_SINGLE: "write single register",
    FC_WRITE_MULTIPLE: "write multiple registers",
}

#: What each exception means on the ZP series (design §2.2).
_EXCEPTIONS: Final = {
    0x01: "illegal function",
    0x02: "illegal data address: the address is outside the map (the bench unit also "
    "answers FC01 and FC02 this way)",
    0x03: "illegal data value: too many words, or a read that crosses the end of a region",
}

_FRAME_BYTES: Final = 8  # a read request, a single write, a multiple-write reply


def _table(fc: int) -> RegisterTable:
    return RegisterTable.INPUT if fc == FC_READ_INPUT else RegisterTable.HOLDING


def _address(table: RegisterTable, address: int) -> str:
    return f"{address:04X}h ({table.number_base + address})"


def _names(table: RegisterTable, start: int, count: int) -> list[str]:
    names: list[str] = []
    for address in range(start, start + count):
        spec = REGISTRY.at(table, address)
        if spec is not None and spec.name not in names:
            names.append(spec.name)
    return names


def _registers(table: RegisterTable, start: int, words: Sequence[int]) -> list[object]:
    """Name and decode each register whose words all lie in ``words``."""
    bank = dict(zip(range(start, start + len(words)), words, strict=True))
    out: list[object] = []
    for spec in REGISTRY.in_table(table):
        if not start <= spec.address <= spec.last_address < start + len(words):
            continue
        spec_words = words_of(bank, spec.address, spec.count)
        entry: dict[str, object] = {"address": _address(table, spec.address), "name": spec.name}
        try:
            decoded = decode_register(spec, spec_words)
        except FujiDecodeError as exc:
            entry["error"] = str(exc)
            out.append(entry)
            continue
        entry["raw"] = decoded.raw
        if spec.name.startswith("reading.ch") and spec.name.endswith(".value"):
            dp_at, unit_at = spec.address + 1, spec.address + 2
            if unit_at in bank and 0 <= bank[dp_at] <= 3:  # noqa: PLR2004
                value = scale(decode_int(bank[spec.address], signed=True), bank[dp_at])
                entry["value"] = f"{value:.{bank[dp_at]}f} {unit_from_code(bank[unit_at]).value}"
        elif decoded.value is None:
            entry["value"] = "needs the range's decimal point"
        elif isinstance(decoded.value, Enum) or decoded.value != decoded.raw or decoded.unit:
            entry["value"] = f"{show(decoded.value)}{f' {decoded.unit}' if decoded.unit else ''}"
        out.append(entry)
    return out


def _write_note(fc: int, start: int, count: int, values: Sequence[int] | None = None) -> str:
    if envelope_allows(fc, start, count, values=values):
        return "inside the write envelope"
    return "outside the write envelope: fujilib never sends this"


def describe_frame(
    frame: bytes,
    *,
    start: int | None = None,
    request: bytes | None = None,
) -> dict[str, object]:
    """Describe one RTU frame.

    A read reply is decoded against ``request`` when given, else against ``start``.

    Raises:
        FujiDecodeError: the frame is too short to be a MODBUS RTU frame.
    """
    if len(frame) < 5:  # noqa: PLR2004
        msg = f"{len(frame)} bytes is too short for a MODBUS RTU frame"
        raise FujiDecodeError(msg, context=ErrorContext(response=frame))
    station, fc = frame[0], frame[1]
    report: dict[str, object] = {
        "frame": frame.hex(" ").upper(),
        "station": station,
        "crc": "valid" if verify_crc(frame) else "INVALID",
    }
    body = frame[2:-2]
    if fc & 0x80:
        report["function"] = f"{fc & 0x7F:02X} {_FUNCTIONS.get(fc & 0x7F, 'unknown')}"
        report["kind"] = "exception"
        code = body[0] if body else None
        report["exception"] = (
            f"{code:02X} {_EXCEPTIONS.get(code, 'unknown')}" if code is not None else "-"
        )
        return report
    report["function"] = f"{fc:02X} {_FUNCTIONS.get(fc, 'not used by the ZP series')}"
    table = _table(fc)
    if fc in {FC_READ_HOLDING, FC_READ_INPUT}:
        if len(frame) == _FRAME_BYTES:
            first, count = int.from_bytes(body[0:2]), int.from_bytes(body[2:4])
            report |= {
                "kind": "request",
                "start": _address(table, first),
                "count": count,
                "registers": _names(table, first, count),
            }
            return report
        words = [int.from_bytes(body[i : i + 2]) for i in range(1, len(body) - 1, 2)]
        report |= {"kind": "reply", "byte_count": body[0], "words": words}
        if request is not None and len(request) == _FRAME_BYTES:
            start = int.from_bytes(request[2:4])
        if start is not None:
            report["start"] = _address(table, start)
            report["registers"] = _registers(table, start, words)
        return report
    if fc == FC_WRITE_SINGLE:
        address, value = int.from_bytes(body[0:2]), int.from_bytes(body[2:4])
        report |= {"kind": "request or echo", "start": _address(table, address), "value": value}
        if address == KEY_SIMULATION_ADDRESS:
            report["meaning"] = f"key simulation: {KeyCode(value).name or value}"
        else:
            operation = next((o.name for o in OPERATIONS.values() if o.address == address), None)
            report["meaning"] = operation or ", ".join(_names(table, address, 1)) or "unmapped"
        report["write_envelope"] = _write_note(fc, address, 1, (value,))
        return report
    if fc == FC_WRITE_MULTIPLE:
        address, count = int.from_bytes(body[0:2]), int.from_bytes(body[2:4])
        report |= {"start": _address(table, address), "count": count}
        if len(frame) == _FRAME_BYTES:
            report["kind"] = "reply"
        else:
            values = [int.from_bytes(body[i : i + 2]) for i in range(5, len(body) - 1, 2)]
            report |= {
                "kind": "request",
                "values": values,
                "registers": _names(table, address, count),
            }
            report["write_envelope"] = _write_note(fc, address, count, values)
        return report
    return report


# --- Register dumps -------------------------------------------------------------------------


def _bank(data: Mapping[str, object], table: str) -> dict[int, int]:
    raw = data.get(table, {})
    if not isinstance(raw, dict):
        msg = f"the dump's {table!r} entry is not an address -> word mapping"
        raise FujiValidationError(msg)
    return {int(str(a), 16): int(w) for a, w in raw.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _section(report: dict[str, object], name: str, build: Callable[[], object]) -> None:
    try:
        report[name] = build()
    except FujiDecodeError as exc:
        report[name] = f"not decodable from this dump: {exc}"


def _captured_at(data: Mapping[str, object]) -> datetime:
    text = data.get("captured_utc")
    if isinstance(text, dict):
        text = cast("dict[str, object]", text).get("input")
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return datetime.fromtimestamp(0, UTC)


def describe_dump(
    data: Mapping[str, object], *, asserted: Mapping[ChannelId, Gas] | None = None
) -> dict[str, object]:
    """Decode a register dump into a report; sections the dump lacks say so."""
    holding, inputs = _bank(data, "holding"), _bank(data, "input")
    report: dict[str, object] = {}
    identity = None
    try:
        identity = decode_identity(inputs)
    except FujiDecodeError as exc:
        report["identity"] = f"not decodable from this dump: {exc}"
    else:
        report["identity"] = type_code_report(*identity)
    channels: tuple[ChannelInfo, ...] = ()
    try:
        present = nonzero_channels(inputs)
    except FujiDecodeError as exc:
        report["channels"] = f"not decodable from this dump: {exc}"
    else:
        channels = label_channels(
            present, asserted=asserted, type_code=identity[0] if identity else None
        )
        report["channels"] = channels_report(channels)
    _section(report, "ranges", lambda: ranges_report(decode_ranges(inputs)))
    at = _captured_at(data)
    timing = TransferTiming(at, at, 0, 0)
    _section(
        report,
        "readings",
        lambda: frame_report(
            decode_frame(inputs, channels, readings_timing=timing, status_timing=timing)
        ),
    )
    _section(report, "error_log", lambda: error_log_report(decode_error_log(inputs)))
    _section(report, "clock", lambda: decode_clock(words_of(inputs, 0x3E8, 7)).isoformat(sep=" "))
    _section(
        report,
        "adc",
        lambda: adc_report(decode_adc(words_of(inputs, 0x3EF, 42), received_at=at, t_mono_ns=0)),
    )
    _section(report, "settings", lambda: _settings(holding, inputs, channels, at))
    return report


def _settings(holding: Bank, inputs: Bank, channels: Sequence[ChannelInfo], at: datetime) -> object:
    meta = decode_metadata(
        holding,
        serial_number=decode_identity(inputs)[1],
        ranges=decode_ranges(inputs),
        channels=channels,
        current_range=decode_current_ranges(inputs),
        captured_at=at,
    )
    return metadata_report(meta)


# --- Entry point -----------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    if args.hex is not None:
        try:
            frame = hex_to_bytes(" ".join(args.hex))
        except FujiValidationError as exc:
            sys.stderr.write(f"error: --hex: {exc}\n")
            return 2
        report: object = describe_frame(frame, start=args.start)
    elif args.fixture is not None:
        exchanges = parse_arrow_fixture(Path(args.fixture))
        report = [
            {
                "line": e.line,
                **({"comment": e.comment} if e.comment else {}),
                "request": describe_frame(e.request),
                "reply": describe_frame(e.response, request=e.request) if e.response else None,
            }
            for e in exchanges
        ]
    else:
        data = json.loads(Path(args.dump).read_text(encoding="utf-8"))
        report = describe_dump(data, asserted=dict(args.gas or ()))
    sys.stdout.write(render(report, args.format))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuji-decode",
        description="Decode MODBUS frames or a register dump of a Fuji ZP-series analyzer.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--hex", nargs="+", metavar="HEX", help="One frame as hex bytes.")
    source.add_argument("--fixture", metavar="PATH", help="An arrow-format frame file.")
    source.add_argument("--dump", metavar="PATH", help="A register bank as JSON.")
    parser.add_argument(
        "--start",
        type=lambda s: int(s, 0),
        metavar="ADDRESS",
        help="First address of a --hex read reply (e.g. 0x000C).",
    )
    parser.add_argument(
        "--gas",
        action="append",
        type=parse_gas,
        metavar="CHn=GAS",
        help="Assert a channel's gas for --dump (repeatable), e.g. CH3=o2.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: returns the exit code (0 ok, 1 decode error, 2 bad arguments)."""
    args = _build_parser().parse_args(argv)
    return run_cli(lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
