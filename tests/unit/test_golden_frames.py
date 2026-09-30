"""The MODBUS manual's worked frames encode and decode byte-exactly (design §2.10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from anymodbus.crc import verify_crc
from anymodbus.framer import encode_adu
from anymodbus.pdu import (
    decode_read_holding_registers_response,
    decode_read_input_registers_response,
    decode_write_multiple_registers_response,
    decode_write_single_register_response,
    encode_read_holding_registers_request,
    encode_read_input_registers_request,
    encode_write_multiple_registers_request,
    encode_write_single_register_request,
)

from fujilib.devices.decode import decode_register
from fujilib.errors import FujiValidationError
from fujilib.protocol.modbus.codec import scale
from fujilib.registry.regions import FC_WRITE_MULTIPLE, FC_WRITE_SINGLE
from fujilib.registry.registers import REGISTRY
from fujilib.registry.units import Unit, unit_from_code
from fujilib.registry.write_policy import KEY_SIMULATION_ADDRESS, envelope_allows
from fujilib.testing import Exchange, hex_to_bytes, parse_arrow_fixture, replay_script

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manual_frames.txt"
EXCHANGES = parse_arrow_fixture(FIXTURE)
CRC_EXAMPLE, FC03, FC04, FC06, FC10 = EXCHANGES


def pdu(adu: bytes) -> bytes:
    return adu[1:-2]


def test_fixture_shape() -> None:
    assert len(EXCHANGES) == 5
    assert CRC_EXAMPLE.response is None
    assert all(e.response is not None for e in EXCHANGES[1:])


@pytest.mark.parametrize("frame", [f for e in EXCHANGES for f in (e.request, e.response) if f])
def test_every_frame_has_a_valid_crc(frame: bytes) -> None:
    assert verify_crc(frame)


def test_crc_example() -> None:
    assert CRC_EXAMPLE.request == encode_adu(
        slave_address=1, pdu=encode_write_single_register_request(0x0005, 1000)
    )
    assert CRC_EXAMPLE.request[-2:] == bytes([0x99, 0x75])


def test_fc03_read_calibration_gas() -> None:
    zero = REGISTRY.resolve("calibration_gas.ch2.range1.zero")
    span = REGISTRY.resolve("calibration_gas.ch2.range1.span")
    assert span.address == zero.address + 1
    request = encode_adu(
        slave_address=1, pdu=encode_read_holding_registers_request(zero.address, 2)
    )
    assert request == FC03.request
    assert FC03.response is not None
    words = decode_read_holding_registers_response(pdu(FC03.response))
    assert words == (0, 1000)
    # The manual's example range has one decimal and unit ppm.
    values = [
        decode_register(s, (w,), scaling=(1, Unit.PPM))
        for s, w in zip((zero, span), words, strict=True)
    ]
    assert [v.value for v in values] == [0.0, 100.0]
    assert {v.unit for v in values} == {"ppm"}


def test_fc04_read_concentration() -> None:
    value = REGISTRY.resolve("reading.ch5.value")
    request = encode_adu(slave_address=1, pdu=encode_read_input_registers_request(value.address, 3))
    assert request == FC04.request
    assert FC04.response is not None
    raw, decimals, unit = decode_read_input_registers_response(pdu(FC04.response))
    assert (raw, decimals, unit) == (1200, 2, 0)
    assert scale(raw, decimals) == 12.0
    assert unit_from_code(unit) is Unit.VOL_PERCENT


def test_fc06_zero_key_is_inside_the_write_envelope_only_as_a_calibration_key() -> None:
    request = encode_adu(
        slave_address=1, pdu=encode_write_single_register_request(KEY_SIMULATION_ADDRESS, 0x40)
    )
    assert request == FC06.request
    assert FC06.response == FC06.request  # the reply echoes
    assert FC06.response is not None
    assert decode_write_single_register_response(pdu(FC06.response)) == (0x07D0, 0x40)
    assert envelope_allows(FC_WRITE_SINGLE, KEY_SIMULATION_ADDRESS, values=(0x40,))
    assert not envelope_allows(FC_WRITE_SINGLE, KEY_SIMULATION_ADDRESS)  # no value: refused
    assert not envelope_allows(FC_WRITE_SINGLE, KEY_SIMULATION_ADDRESS, values=(0x01,))


def test_fc10_write_alarm_limits() -> None:
    first = REGISTRY.resolve("alarm1.range1.high")
    names = [
        s.name
        for s in REGISTRY.in_table(first.table)
        if first.address <= s.address < first.address + 4
    ]
    assert names == [
        "alarm1.range1.high",
        "alarm1.range1.low",
        "alarm1.range2.high",
        "alarm1.range2.low",
    ]
    request = encode_adu(
        slave_address=1,
        pdu=encode_write_multiple_registers_request(first.address, [5000, 10, 1000, 10]),
    )
    assert request == FC10.request
    assert envelope_allows(FC_WRITE_MULTIPLE, first.address, 4)
    assert FC10.response is not None
    assert decode_write_multiple_registers_response(pdu(FC10.response)) == (0x0023, 4)


# --- The arrow parser ------------------------------------------------------------------------


def test_parser_details() -> None:
    text = """
    # a comment line
    > 01 03 00 04 00 02 85 CA      # read
    < 01 03 04                     # reply split over
    < 00 00 03 E8 FA 8D            # two lines
    > 0106000503E89975
    """
    exchanges = parse_arrow_fixture(text)
    assert exchanges[0].response == FC03.response
    assert exchanges[0].comment == "read"
    assert exchanges[0].line == 3
    assert exchanges[1] == Exchange(CRC_EXAMPLE.request, None, 6, "")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("< 01 02", r"<string>:1: a reply before any request"),
        ("> 01 0G", r"<string>:1: .*not a hex byte"),
        ("> 012", r"<string>:1: odd-length"),
        (">", r"<string>:1: no hex bytes"),
        ("= 01 02", r"<string>:1: a line must start"),
        ("> 01\n? 02", r"<string>:2: "),
    ],
)
def test_parser_errors(text: str, match: str) -> None:
    with pytest.raises(FujiValidationError, match=match):
        parse_arrow_fixture(text)


def test_parser_names_the_source() -> None:
    with pytest.raises(FujiValidationError, match=r"^frames\.txt:1:"):
        parse_arrow_fixture("< 01", name="frames.txt")


def test_hex_separators() -> None:
    assert hex_to_bytes("01:02,03 0405") == bytes([1, 2, 3, 4, 5])


def test_replay_script() -> None:
    script = replay_script(EXCHANGES)
    assert len(script) == 4  # the CRC example has no reply
    assert script[FC04.request] == FC04.response
    conflicting = (*EXCHANGES, Exchange(FC04.request, b"\x01", 99, ""))
    with pytest.raises(FujiValidationError, match="line 99"):
        replay_script(conflicting)
    assert replay_script((*EXCHANGES, FC04)) == script
