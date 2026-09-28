"""Measurement units and the unit-code registers (design §2.5).

A concentration's unit is not fixed: it lives in a register beside the value
(input 30001–30036) or in the range tables (31067–31076), as a code 0–3. The
unit of a range cannot be changed (TN5A1190a p.26).

:func:`unit_from_code` and :func:`coerce_unit` never raise: an unrecognised
code or text becomes :attr:`Unit.UNKNOWN`, so one odd register never sinks a
whole frame. The pint mapping lives in :mod:`fujilib.units`.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.errors import FujiValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Unit", "coerce_unit", "unit_code", "unit_from_code"]


class Unit(StrEnum):
    """A concentration unit. The value is the canonical display string."""

    VOL_PERCENT = "vol%"
    PPM = "ppm"
    MG_M3 = "mg/m3"
    G_M3 = "g/m3"
    UNKNOWN = "?"


_BY_CODE: Final[Mapping[int, Unit]] = MappingProxyType(
    {0: Unit.VOL_PERCENT, 1: Unit.PPM, 2: Unit.MG_M3, 3: Unit.G_M3}
)
_CODE_OF: Final[Mapping[Unit, int]] = MappingProxyType({u: c for c, u in _BY_CODE.items()})

_BY_TEXT: Final[Mapping[str, Unit]] = MappingProxyType(
    {
        "vol%": Unit.VOL_PERCENT,
        "vol %": Unit.VOL_PERCENT,
        "%": Unit.VOL_PERCENT,
        "percent": Unit.VOL_PERCENT,
        "ppm": Unit.PPM,
        "mg/m3": Unit.MG_M3,
        "mg/m^3": Unit.MG_M3,
        "mg/m**3": Unit.MG_M3,
        "mg/m³": Unit.MG_M3,
        "g/m3": Unit.G_M3,
        "g/m^3": Unit.G_M3,
        "g/m**3": Unit.G_M3,
        "g/m³": Unit.G_M3,
        "?": Unit.UNKNOWN,
    }
)


def unit_from_code(code: int) -> Unit:
    """Map a unit-code register value (0 vol%, 1 ppm, 2 mg/m³, 3 g/m³) to a :class:`Unit`.

    Any other value becomes :attr:`Unit.UNKNOWN`.
    """
    return _BY_CODE.get(code, Unit.UNKNOWN)


def unit_code(unit: Unit) -> int:
    """Return the register code for ``unit``.

    Raises:
        FujiValidationError: ``unit`` is :attr:`Unit.UNKNOWN`, which has no code.
    """
    try:
        return _CODE_OF[unit]
    except KeyError:
        msg = f"unit {unit.value!r} has no register code"
        raise FujiValidationError(msg) from None


def coerce_unit(unit: Unit | str) -> Unit:
    """Map a :class:`Unit` or a unit string (case-insensitive) to a :class:`Unit`.

    Unrecognised text becomes :attr:`Unit.UNKNOWN`.
    """
    if isinstance(unit, Unit):
        return unit
    return _BY_TEXT.get(unit.strip().casefold(), Unit.UNKNOWN)
