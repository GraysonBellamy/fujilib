"""The word a setting write sends, from the caller's value (design §6.1, §6.3).

The inverse of :func:`fujilib.devices.decode.decode_register`, in two steps,
because a range-scaled value can only be finished once its range has been
read, immediately before the write:

1. :func:`prepare_value` checks everything that needs no I/O: that the
   register may be written, the value's type, an enum member, a finite number,
   and the unit a scaled value is given in. An unscaled value is encoded here
   and its limits checked.
2. :func:`encode_prepared` finishes a range-scaled value against that range:
   the unit must be the range's, the value must fit the range's decimals
   exactly, and the raw and percent-of-full-scale limits must hold.

Nothing here does I/O, and every refusal is a
:class:`~fujilib.errors.FujiValidationError`: nothing was sent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from fujilib.errors import ErrorContext, FujiValidationError
from fujilib.protocol.modbus.codec import DataType, unscale
from fujilib.registry.registers import ScalingKind
from fujilib.registry.units import Unit, coerce_unit
from fujilib.registry.write_policy import REVIEWED_SETTINGS

if TYPE_CHECKING:
    from enum import IntEnum

    from fujilib.registry.registers import RegisterSpec

__all__ = ["PreparedValue", "encode_prepared", "prepare_value"]


@dataclass(frozen=True, slots=True)
class PreparedValue:
    """A caller's value for one setting, checked as far as it can be without I/O."""

    spec: RegisterSpec
    value: bool | int | IntEnum | Decimal
    """The value, normalized: a flag, an enum member, an integer, or an exact decimal."""
    raw: int | None
    """The word to write; ``None`` for a range-scaled value, which needs its range first."""
    unit: Unit | None = None
    """The unit a range-scaled value is given in."""


def _refuse(spec: RegisterSpec, message: str, **extra: object) -> FujiValidationError:
    return FujiValidationError(
        f"{spec.name}: {message}",
        context=ErrorContext(register_address=spec.address, extra={"setting": spec.name, **extra}),
    )


def prepare_value(
    spec: RegisterSpec, value: object, *, unit: Unit | str | None = None
) -> PreparedValue:
    """Check ``value`` for ``spec`` without I/O, and encode it unless it is range-scaled.

    Args:
        spec: The register, which must be writable.
        value: ``True``/``False`` for a flag; for an enumerated setting a
            member of its enum or its name (any case), never its number; an
            integer for a count or a time; for a range-scaled setting (a
            calibration gas) a number, in ``unit``.
        unit: The unit of a range-scaled value; required there, and refused
            unless it is the range's own. For an unscaled value it may be given,
            and must then be the register's (``"s"``, ``"%FS"``).

    Raises:
        FujiValidationError: the register is read-only, or the value or unit
            does not fit it.
    """
    if not spec.writable or (spec.name, spec.address) not in REVIEWED_SETTINGS:
        raise _refuse(spec, "the register is read-only")
    if spec.scaling.kind is ScalingKind.BY_RANGE:
        return _prepare_scaled(spec, value, unit)
    if unit is not None and (spec.unit is None or str(unit).strip() != spec.unit):
        raise _refuse(spec, f"takes no unit {unit!r}; its unit is {spec.unit or 'none'}")
    if spec.dtype is DataType.BOOL:
        if not isinstance(value, bool):
            raise _refuse(spec, f"expects True or False, got {value!r}")
        return _encoded(spec, value, int(value))
    if spec.enum is not None:
        member = _member(spec, spec.enum, value)
        return _encoded(spec, member, int(member))
    number = _whole(spec, value)
    return _encoded(spec, number, number)


def encode_prepared(
    prepared: PreparedValue, range_info: tuple[Unit, float, int] | None = None
) -> int:
    """The word to write for ``prepared``.

    ``range_info`` is the ``(unit, full scale, decimals)`` of a range-scaled
    setting's (channel, range), read just before the write; it is ignored for
    an unscaled one.

    Raises:
        FujiValidationError: the range is unknown or in another unit, the
            value has more decimals than the range, or it is outside the
            limits.
    """
    if prepared.raw is not None:
        return prepared.raw
    spec = prepared.spec
    if range_info is None:
        raise _refuse(spec, "the range's decimal point and unit are not known")
    range_unit, full_scale, decimals = range_info
    if range_unit is Unit.UNKNOWN or range_unit is not prepared.unit:
        given = prepared.unit.value if prepared.unit is not None else "?"
        raise _refuse(
            spec,
            f"Ch{spec.channel.number if spec.channel else '?'} range {spec.range} is in "
            f"{range_unit.value}, not {given}",
            range_unit=range_unit.value,
        )
    raw = unscale(prepared.value, decimals)
    _check_limits(spec, raw)
    percent = spec.write_percent_fs
    if percent is not None:
        raw_full_scale = unscale(full_scale, decimals) if math.isfinite(full_scale) else 0
        low, high = percent
        if raw_full_scale <= 0 or not low * raw_full_scale <= raw * 100 <= high * raw_full_scale:
            raise _refuse(
                spec,
                f"{prepared.value} {range_unit.value} is outside {low}-{high} % of the range's "
                f"full scale ({full_scale:g} {range_unit.value})",
                raw=raw,
            )
    return raw


def _prepare_scaled(spec: RegisterSpec, value: object, unit: Unit | str | None) -> PreparedValue:
    if unit is None:
        raise _refuse(spec, "give the unit the value is in, e.g. unit='vol%'")
    parsed = coerce_unit(unit)
    if parsed is Unit.UNKNOWN:
        raise _refuse(spec, f"unknown unit {unit!r}")
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise _refuse(spec, f"expects a number, got {value!r}")
    try:
        exact = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        raise _refuse(spec, f"expects a number, got {value!r}") from None
    if not exact.is_finite() or exact < 0:
        raise _refuse(spec, f"expects a finite, non-negative number, got {value!r}")
    return PreparedValue(spec, exact, None, parsed)


def _member(spec: RegisterSpec, enum: type[IntEnum], value: object) -> IntEnum:
    # A bare number is refused: the codes are not what they look like
    # (RangeIndex 1 is range 2), so a member or its name is required.
    if isinstance(value, enum):
        return value
    if isinstance(value, str):
        key = value.strip().upper().replace("-", "_").replace(" ", "_")
        for member in enum:
            if member.name == key:
                return member
    names = ", ".join(m.name.lower() for m in enum)
    raise _refuse(spec, f"expects one of {names} (or a {enum.__name__}); got {value!r}")


def _whole(spec: RegisterSpec, value: object) -> int:
    if isinstance(value, bool):
        raise _refuse(spec, f"expects a whole number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise _refuse(spec, f"expects a whole number, got {value!r}")


def _encoded(spec: RegisterSpec, value: bool | int | IntEnum, raw: int) -> PreparedValue:
    _check_limits(spec, raw)
    return PreparedValue(spec, value, raw)


def _check_limits(spec: RegisterSpec, raw: int) -> None:
    low = spec.minimum if spec.minimum is not None else 0
    high = spec.maximum if spec.maximum is not None else 0xFFFF
    if not low <= raw <= high:
        unit = f" {spec.unit}" if spec.unit else ""
        raise _refuse(spec, f"{raw}{unit} is outside {low}-{high}{unit}", raw=raw)
    if spec.write_values is not None and raw not in spec.write_values:
        allowed = ", ".join(_name(spec, v) for v in sorted(spec.write_values))
        raise _refuse(spec, f"{_name(spec, raw)} cannot be written; only {allowed}", raw=raw)


def _name(spec: RegisterSpec, raw: int) -> str:
    return spec.enum(raw).name.lower() if spec.enum is not None else str(raw)
