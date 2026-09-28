"""Pint-compatible unit strings — :func:`to_pint` (unified API §K).

Every sibling library exposes the same free function
``to_pint(unit) -> str | None`` in ``<lib>.units``, so consumers can resolve
units without knowing which device produced a row. ``pint`` is **not** a
dependency: this returns plain strings, and the consumer parses them.

The strings are chosen so ``capa``'s unit registry parses and canonicalizes
them. ``vol%`` is a volume fraction in percent, so it maps to ``"percent"``;
``ppm`` maps to ``"ppm"``. The two are dimensionally compatible and differ by
10⁴, so a consumer must compare canonical unit strings, not only dimensions.
``to_pint`` never converts values.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.registry.units import Unit, coerce_unit

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Unit", "to_pint"]


#: Every :class:`Unit` member maps to a pint string, or ``None`` where pint has
#: no meaning for it.
_UNIT_TO_PINT: Final[Mapping[Unit, str | None]] = MappingProxyType(
    {
        Unit.VOL_PERCENT: "percent",
        Unit.PPM: "ppm",
        Unit.MG_M3: "mg/m**3",
        Unit.G_M3: "g/m**3",
        Unit.UNKNOWN: None,
    }
)


def _check_unit_coverage() -> None:
    """Fail at import if a :class:`Unit` member has no mapping, not even ``None``."""
    missing = [u for u in Unit if u not in _UNIT_TO_PINT]
    if missing:  # pragma: no cover — guarded by the unit tests
        msg = f"Unit members missing from fujilib.units._UNIT_TO_PINT: {missing!r}"
        raise RuntimeError(msg)


_check_unit_coverage()


def to_pint(unit: Unit | str | None) -> str | None:
    """Return a pint-compatible unit string for ``unit``, or ``None``.

    Accepts a :class:`Unit`, a unit string such as ``"vol%"`` or ``"ppm"``, or
    ``None``. Unknown strings, :attr:`Unit.UNKNOWN` and ``None`` return
    ``None``; this never raises.

    Example::

        >>> to_pint(Unit.VOL_PERCENT)
        'percent'
        >>> to_pint("mg/m3")
        'mg/m**3'
        >>> to_pint("furlongs")
    """
    if unit is None:
        return None
    return _UNIT_TO_PINT[coerce_unit(unit)]
