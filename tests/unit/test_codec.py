"""Register-word codec: round trips, bench vectors and rejections (design §2.5)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fujilib.errors import FujiDecodeError, FujiValidationError
from fujilib.protocol.modbus.codec import (
    DataType,
    as_decimal,
    decode_bcd,
    decode_bool,
    decode_chars,
    decode_enum,
    decode_int,
    decode_raw,
    decode_uint32_lh,
    encode_bcd,
    encode_chars,
    encode_int,
    encode_uint32_lh,
    scale,
    unscale,
)
from fujilib.registry.enums import AlarmState

words16 = st.integers(min_value=0, max_value=0xFFFF)
concentrations = st.integers(min_value=-9999, max_value=9999)
decimals = st.integers(min_value=0, max_value=3)

# --- Integers ------------------------------------------------------------


@given(st.integers(min_value=-0x8000, max_value=0x7FFF))
def test_int16_round_trip(value: int) -> None:
    assert decode_int(encode_int(value, signed=True), signed=True) == value


@given(words16)
def test_uint16_round_trip(word: int) -> None:
    assert encode_int(decode_int(word, signed=False), signed=False) == word


def test_negative_concentration_is_twos_complement() -> None:
    # Bench: CO2 read FFF5h = -11 (design §2.5).
    assert decode_int(0xFFF5, signed=True) == -11


@pytest.mark.parametrize(("value", "signed"), [(0x8000, True), (-1, False), (0x10000, False)])
def test_encode_int_rejects_overflow(value: int, *, signed: bool) -> None:
    with pytest.raises(FujiValidationError):
        encode_int(value, signed=signed)


@given(st.integers(min_value=0, max_value=0xFFFF_FFFF))
def test_uint32_low_word_first_round_trip(value: int) -> None:
    words = encode_uint32_lh(value)
    assert words == (value & 0xFFFF, value >> 16)
    assert decode_uint32_lh(words) == value


def test_long_word_bench_vector() -> None:
    # Bench: 00A4h-00A5h read (16960, 15) = 1,000,000 (design §2.6).
    assert decode_uint32_lh((16960, 15)) == 1_000_000


def test_encode_uint32_rejects_negative() -> None:
    with pytest.raises(FujiValidationError):
        encode_uint32_lh(-1)


@pytest.mark.parametrize("words", [(), (1,), (1, 2, 3), (0x10000, 0)])
def test_decode_uint32_rejects_bad_words(words: tuple[int, ...]) -> None:
    with pytest.raises(FujiDecodeError):
        decode_uint32_lh(words)


def test_decode_rejects_out_of_range_word() -> None:
    with pytest.raises(FujiDecodeError):
        decode_int(-1, signed=False)


@pytest.mark.parametrize(("word", "expected"), [(0, False), (1, True), (2, True)])
def test_bool_is_nonzero(word: int, *, expected: bool) -> None:
    assert decode_bool(word) is expected


# --- BCD -------------------------------------------------------------------


@given(st.integers(min_value=0, max_value=9999))
def test_bcd_round_trip(value: int) -> None:
    assert decode_bcd(encode_bcd(value)) == value


def test_bcd_clock_vector() -> None:
    # Bench clock year register read 0x26 for 2026 (design §2.6).
    assert decode_bcd(0x26) == 26
    assert decode_bcd(0x2359) == 2359


@pytest.mark.parametrize("word", [0x000C, 0x00A0, 0xF000])
def test_bcd_rejects_non_decimal_nibble(word: int) -> None:
    # 0x000C is what the bench unit's schedule start hours read (design §2.6).
    with pytest.raises(FujiDecodeError):
        decode_bcd(word)


@pytest.mark.parametrize("value", [-1, 10_000])
def test_encode_bcd_rejects_out_of_range(value: int) -> None:
    with pytest.raises(FujiValidationError):
        encode_bcd(value)


# --- Characters --------------------------------------------------------------


def test_chars_bench_serial() -> None:
    words = tuple(ord(c) for c in "N8A0259T")
    assert decode_chars(words) == "N8A0259T"


def test_chars_keep_positions_of_blanks() -> None:
    words = (ord("Z"), 0, ord("A"), 0, 0)
    assert decode_chars(words, strip=False) == "Z A  "
    assert decode_chars(words) == "Z A"


@pytest.mark.parametrize("word", [0x4100, 0x0141, 0x0007, 0x00FF])
def test_chars_reject_non_character_words(word: int) -> None:
    with pytest.raises(FujiDecodeError):
        decode_chars((word,))


@given(st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), max_size=26))
def test_chars_round_trip(text: str) -> None:
    assert decode_chars(encode_chars(text, 26)) == text


@pytest.mark.parametrize(("text", "width"), [("ABC", 2), ("é", 4)])
def test_encode_chars_rejects(text: str, width: int) -> None:
    with pytest.raises(FujiValidationError):
        encode_chars(text, width)


# --- Enums -------------------------------------------------------------------


def test_decode_enum_known_and_unknown() -> None:
    assert decode_enum(AlarmState, 3) is AlarmState.HIGH_HIGH
    # The bench unit's alarm-6 target reads 12, outside the documented domain.
    assert decode_enum(AlarmState, 12) == 12
    assert type(decode_enum(AlarmState, 12)) is int
    with pytest.raises(FujiDecodeError):
        decode_enum(AlarmState, 12, strict=True)


# --- Scaling -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "dp", "value"),
    [(2029, 2, 20.29), (-11, 2, -0.11), (-9, 3, -0.009), (1200, 2, 12.0), (5, 0, 5.0)],
)
def test_scale_bench_and_manual_vectors(raw: int, dp: int, value: float) -> None:
    result = scale(raw, dp)
    assert result == value
    assert isinstance(result, float)


@given(concentrations, decimals)
def test_scale_unscale_round_trip(raw: int, dp: int) -> None:
    assert unscale(scale(raw, dp), dp) == raw
    assert unscale(as_decimal(raw, dp), dp) == raw


def test_unscale_absorbs_float_noise() -> None:
    assert unscale(0.1 + 0.2, 2) == 30


@pytest.mark.parametrize("value", [12.345, float("nan"), float("inf"), "twelve"])
def test_unscale_rejects(value: float | str) -> None:
    with pytest.raises(FujiValidationError):
        unscale(value, 2)


@pytest.mark.parametrize("dp", [-1, 4])
def test_bad_decimal_point_is_a_decode_error(dp: int) -> None:
    with pytest.raises(FujiDecodeError):
        scale(100, dp)


def test_as_decimal_is_exact() -> None:
    assert as_decimal(2029, 2) == Decimal("20.29")


# --- Dispatch ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "dtype", "expected"),
    [
        ((0xFFF5,), DataType.INT16, -11),
        ((0xFFF5,), DataType.UINT16, 0xFFF5),
        ((3,), DataType.ENUM, 3),
        ((16960, 15), DataType.UINT32_LH, 1_000_000),
        ((0x2359,), DataType.BCD, 2359),
        ((1,), DataType.BOOL, True),
        ((ord("Z"), ord("P")), DataType.CHAR, "ZP"),
    ],
)
def test_decode_raw(words: tuple[int, ...], dtype: DataType, expected: object) -> None:
    assert decode_raw(words, dtype) == expected


def test_decode_raw_checks_width() -> None:
    with pytest.raises(FujiDecodeError):
        decode_raw((1, 2), DataType.UINT16)
    assert DataType.CHAR.fixed_width is None
    assert DataType.UINT32_LH.fixed_width == 2
