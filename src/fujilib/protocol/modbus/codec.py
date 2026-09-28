"""Register-word codecs for the ZP series (design §2.5).

Pure functions between 16-bit register words and Python values. Integer work
is delegated to :mod:`anymodbus.decoders`; this module adds what the ZP series
needs and ``anymodbus`` does not have:

- **BCD** words (the undocumented clock; the manual also says schedule start
  times, which the bench unit contradicts, design §2.6);
- **one ASCII character per register**, in the low byte (type code and serial);
- **decimal-point scaling**: a concentration is a signed integer with its
  decimal places in another register (0–3);
- **total enum decoding**, so an undocumented value can be kept as a plain
  ``int`` instead of failing a whole read.

Long words are **low word first** (:attr:`anymodbus.WordOrder.LOW_HIGH`), big
endian inside each word. A value that fails to decode raises
:class:`~fujilib.errors.FujiDecodeError`; a value that cannot be encoded raises
:class:`~fujilib.errors.FujiValidationError` (design §4.4).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Final, assert_never

from anymodbus import ByteOrder, WordOrder
from anymodbus.decoders import decode_int16, decode_int32, encode_int16, encode_int32

from fujilib.errors import ErrorContext, FujiDecodeError, FujiValidationError
from fujilib.protocol.base import ProtocolKind

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MAX_DECIMALS",
    "DataType",
    "as_decimal",
    "decode_bcd",
    "decode_bool",
    "decode_chars",
    "decode_enum",
    "decode_int",
    "decode_raw",
    "decode_uint32_lh",
    "encode_bcd",
    "encode_chars",
    "encode_int",
    "encode_uint32_lh",
    "scale",
    "unscale",
]

#: The largest decimal-point position the analyzer reports (divide by 10³).
MAX_DECIMALS: Final = 3

#: The tolerance, in raw counts, within which :func:`unscale` accepts a value
#: as exactly representable (absorbs binary floating-point noise such as
#: ``0.1 + 0.2``).
_UNSCALE_TOLERANCE: Final = Decimal("1e-6")

_POWERS_OF_TEN: Final = (1, 10, 100, 1000)
_WORD_MAX: Final = 0xFFFF
_BCD_MAX: Final = 9999
_ASCII_PRINTABLE: Final = range(0x20, 0x7F)


class DataType(StrEnum):
    """How a register's words encode its value."""

    UINT16 = "uint16"
    """Unsigned 16-bit integer."""
    INT16 = "int16"
    """Signed 16-bit integer, two's complement (concentrations, deviations)."""
    UINT32_LH = "uint32_lh"
    """Unsigned 32-bit integer over two words, low word first."""
    BCD = "bcd"
    """Four BCD digits in one word (``0x23`` = 23)."""
    BOOL = "bool"
    """A whole-register flag: 0 off, anything else on."""
    ENUM = "enum"
    """An unsigned code with a documented meaning per value."""
    CHAR = "char"
    """One ASCII character per register, in the low byte."""

    @property
    def fixed_width(self) -> int | None:
        """Words per value, or ``None`` for :attr:`CHAR`, whose width is per register."""
        if self is DataType.UINT32_LH:
            return 2
        if self is DataType.CHAR:
            return None
        return 1


def _ctx(**extra: object) -> ErrorContext:
    return ErrorContext(protocol=ProtocolKind.MODBUS_RTU, extra=extra)


def _check_words(words: Sequence[int], expected: int | None, what: str) -> None:
    if expected is not None and len(words) != expected:
        msg = f"{what} expects {expected} word(s), got {len(words)}"
        raise FujiDecodeError(msg, context=_ctx(words=tuple(words)))
    for word in words:
        if not 0 <= word <= _WORD_MAX:
            msg = f"{what}: register word {word!r} is outside 0..0xFFFF"
            raise FujiDecodeError(msg, context=_ctx(words=tuple(words)))


# --- Integers ------------------------------------------------------------


def decode_int(word: int, *, signed: bool) -> int:
    """Decode one register as a 16-bit integer (two's complement when ``signed``).

    Raises:
        FujiDecodeError: ``word`` is not a 16-bit value.
    """
    _check_words((word,), 1, "int16" if signed else "uint16")
    return decode_int16((word,), signed=signed, byte_order=ByteOrder.BIG)


def encode_int(value: int, *, signed: bool) -> int:
    """Encode ``value`` as one register word.

    Raises:
        FujiValidationError: ``value`` does not fit in 16 bits.
    """
    try:
        (word,) = encode_int16(value, signed=signed, byte_order=ByteOrder.BIG)
    except (ValueError, TypeError, OverflowError) as exc:
        kind = "int16" if signed else "uint16"
        msg = f"{value!r} does not fit in an {kind} register"
        raise FujiValidationError(msg, context=_ctx(value=value)) from exc
    return word


def decode_uint32_lh(words: Sequence[int]) -> int:
    """Decode a long word: two registers, low-order word first (design §2.5).

    Raises:
        FujiDecodeError: not exactly two 16-bit words.
    """
    _check_words(words, 2, "uint32 (low word first)")
    return decode_int32(
        tuple(words), signed=False, word_order=WordOrder.LOW_HIGH, byte_order=ByteOrder.BIG
    )


def encode_uint32_lh(value: int) -> tuple[int, int]:
    """Encode ``value`` as a long word, low-order word first.

    Raises:
        FujiValidationError: ``value`` does not fit in 32 unsigned bits.
    """
    try:
        return encode_int32(
            value, signed=False, word_order=WordOrder.LOW_HIGH, byte_order=ByteOrder.BIG
        )
    except (ValueError, TypeError, OverflowError) as exc:
        msg = f"{value!r} does not fit in an unsigned long word"
        raise FujiValidationError(msg, context=_ctx(value=value)) from exc


def decode_bool(word: int) -> bool:
    """Decode a whole-register flag: 0 is off, anything else on.

    Any non-zero value counts as set, so an unexpected value in a status flag
    errs towards "held" or "calibrating", which marks data invalid rather than
    valid.

    Raises:
        FujiDecodeError: ``word`` is not a 16-bit value.
    """
    _check_words((word,), 1, "bool")
    return word != 0


# --- BCD -----------------------------------------------------------------


def decode_bcd(word: int) -> int:
    """Decode four BCD digits (``0x2359`` → 2359).

    Raises:
        FujiDecodeError: a nibble is above 9, or ``word`` is not a 16-bit value.
    """
    _check_words((word,), 1, "BCD")
    value = 0
    for shift in (12, 8, 4, 0):
        digit = (word >> shift) & 0xF
        if digit > 9:  # noqa: PLR2004
            msg = f"0x{word:04X} is not a BCD value"
            raise FujiDecodeError(msg, context=_ctx(word=word))
        value = value * 10 + digit
    return value


def encode_bcd(value: int) -> int:
    """Encode 0–9999 as four BCD digits (2359 → ``0x2359``).

    Raises:
        FujiValidationError: ``value`` is outside 0–9999.
    """
    if not 0 <= value <= _BCD_MAX:
        msg = f"{value!r} is outside the BCD range 0-{_BCD_MAX}"
        raise FujiValidationError(msg, context=_ctx(value=value))
    word = 0
    for shift, digit in zip((12, 8, 4, 0), f"{value:04d}", strict=True):
        word |= int(digit) << shift
    return word


# --- Characters ----------------------------------------------------------


def decode_chars(words: Sequence[int], *, strip: bool = True) -> str:
    """Decode one ASCII character per register, from the low byte (design §2.5).

    A zero low byte is a blank and becomes a space, so positions are kept (the
    type code is decoded by digit position). With ``strip`` the result has
    trailing blanks removed.

    Raises:
        FujiDecodeError: a high byte is non-zero or a character is not printable
            ASCII. Either means the block was not a character field, which a
            probe treats as invalid data rather than a string.
    """
    _check_words(words, None, "characters")
    chars: list[str] = []
    for word in words:
        high, low = word >> 8, word & 0xFF
        if high != 0 or not (low == 0 or low in _ASCII_PRINTABLE):
            msg = f"register 0x{word:04X} is not one ASCII character in the low byte"
            raise FujiDecodeError(msg, context=_ctx(words=tuple(words)))
        chars.append(" " if low == 0 else chr(low))
    text = "".join(chars)
    return text.rstrip() if strip else text


def encode_chars(text: str, width: int) -> tuple[int, ...]:
    """Encode ``text`` as one character per register, padded with blanks to ``width``.

    Raises:
        FujiValidationError: ``text`` is longer than ``width`` or not printable ASCII.
    """
    if len(text) > width or any(ord(c) not in _ASCII_PRINTABLE for c in text):
        msg = f"{text!r} is not up to {width} printable ASCII characters"
        raise FujiValidationError(msg, context=_ctx(text=text))
    return tuple(ord(c) for c in text) + (0,) * (width - len(text))


# --- Enums ---------------------------------------------------------------


def decode_enum[E: IntEnum](enum: type[E], raw: int, *, strict: bool = False) -> E | int:
    """Decode ``raw`` as a member of ``enum``.

    A value the manual does not define is returned as a plain ``int`` so it is
    kept, not lost; with ``strict`` it raises instead.

    Raises:
        FujiDecodeError: ``strict`` and ``raw`` is not a member of ``enum``.
    """
    try:
        return enum(raw)
    except ValueError:
        if strict:
            msg = f"{raw!r} is not a documented {enum.__name__} value"
            raise FujiDecodeError(msg, context=_ctx(value=raw)) from None
        return raw


# --- Decimal-point scaling -------------------------------------------------


def _check_decimals(decimals: int) -> None:
    if not 0 <= decimals <= MAX_DECIMALS:
        msg = f"decimal-point position {decimals!r} is outside 0-{MAX_DECIMALS}"
        raise FujiDecodeError(msg, context=_ctx(decimals=decimals))


def scale(raw: int, decimals: int) -> float:
    """Apply a decimal-point position: ``scale(2029, 2)`` → ``20.29``.

    Always returns a ``float``, so a concentration column never flips between
    ``int`` and ``float`` when the decimal point is 0.

    Raises:
        FujiDecodeError: ``decimals`` is outside 0–3.
    """
    _check_decimals(decimals)
    return raw / _POWERS_OF_TEN[decimals]


def as_decimal(raw: int, decimals: int) -> Decimal:
    """Return the exact decimal value of ``raw`` at ``decimals`` places.

    Raises:
        FujiDecodeError: ``decimals`` is outside 0–3.
    """
    _check_decimals(decimals)
    return Decimal(raw).scaleb(-decimals)


def unscale(value: float | int | Decimal | str, decimals: int) -> int:
    """Invert :func:`scale`: ``unscale(20.29, 2)`` → ``2029``.

    Raises:
        FujiValidationError: ``value`` is not finite, or has more precision
            than ``decimals`` places can hold.
        FujiDecodeError: ``decimals`` is outside 0–3.
    """
    _check_decimals(decimals)
    try:
        exact = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        msg = f"{value!r} is not a number"
        raise FujiValidationError(msg, context=_ctx(value=value)) from exc
    if not exact.is_finite():
        msg = f"{value!r} is not finite"
        raise FujiValidationError(msg, context=_ctx(value=value))
    shifted = exact.scaleb(decimals)
    rounded = shifted.to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(shifted - rounded) > _UNSCALE_TOLERANCE:
        msg = f"{value!r} has more than {decimals} decimal place(s)"
        raise FujiValidationError(msg, context=_ctx(value=value, decimals=decimals))
    return int(rounded)


# --- Generic dispatch --------------------------------------------------------


def decode_raw(words: Sequence[int], data_type: DataType) -> int | bool | str:
    """Decode ``words`` by ``data_type`` without scaling or enum lookup.

    :attr:`DataType.ENUM` returns the raw code. Scaling and enum meaning need
    the register's spec and live above the codec.

    Raises:
        FujiDecodeError: wrong width or an undecodable value.
    """
    width = data_type.fixed_width
    _check_words(words, width, data_type.value)
    match data_type:
        case DataType.UINT16 | DataType.ENUM:
            return decode_int(words[0], signed=False)
        case DataType.INT16:
            return decode_int(words[0], signed=True)
        case DataType.UINT32_LH:
            return decode_uint32_lh(words)
        case DataType.BCD:
            return decode_bcd(words[0])
        case DataType.BOOL:
            return decode_bool(words[0])
        case DataType.CHAR:
            return decode_chars(words)
        case _:  # pragma: no cover — every DataType has a case
            assert_never(data_type)
