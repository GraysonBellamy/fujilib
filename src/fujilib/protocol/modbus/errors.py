"""Translation of ``anymodbus`` and ``anyserial`` exceptions to :mod:`fujilib.errors` (design §4.6).

Applied at a single boundary, in the Modbus client, always as
``raise mapped from exc`` so the original exception stays reachable.

Order matters. ``anymodbus``'s ``ProtocolError`` and ``ConfigurationError``
are also ``ValueError``; its ``FrameTimeoutError`` is also a ``TimeoutError``,
which is an ``OSError``. The Modbus classes are therefore matched before the
built-in ones. ``anyserial`` raises its own ``SerialError`` (an ``OSError``)
for port failures that ``anymodbus`` does not translate, and those map to a
connection error. A bare ``ValueError`` from ``anymodbus`` means an argument
slipped past fujilib's own checks: a bug, reported as a configuration error
(design §4.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import anyio
from anymodbus import (
    BusClosedError,
    ChecksumError,
    ConnectionLostError,
    FrameError,
    FrameTimeoutError,
    IllegalDataAddressError,
    IllegalDataValueError,
    IllegalFunctionError,
    ModbusError,
    ModbusExceptionResponse,
    ModbusUnsupportedFunctionError,
    ProtocolError,
)
from anymodbus import ConfigurationError as ModbusConfigurationError

from fujilib.errors import (
    ErrorContext,
    FujiConfigurationError,
    FujiConnectionError,
    FujiError,
    FujiFrameError,
    FujiModbusError,
    FujiModbusIllegalDataAddressError,
    FujiModbusIllegalDataValueError,
    FujiModbusIllegalFunctionError,
    FujiModbusTimeoutError,
    FujiProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["MAPPED_EXCEPTIONS", "map_modbus_error"]

#: Every exception type :func:`map_modbus_error` translates.
MAPPED_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    ModbusError,
    OSError,
    ValueError,
    anyio.BrokenResourceError,
    anyio.BusyResourceError,
    anyio.ClosedResourceError,
)

# (anymodbus / anyserial type, fujilib type), first match wins.
_RULES: Final[Sequence[tuple[tuple[type[BaseException], ...], type[FujiError]]]] = (
    ((IllegalFunctionError,), FujiModbusIllegalFunctionError),
    ((IllegalDataAddressError,), FujiModbusIllegalDataAddressError),
    ((IllegalDataValueError,), FujiModbusIllegalDataValueError),
    ((ModbusExceptionResponse,), FujiModbusError),
    ((FrameTimeoutError,), FujiModbusTimeoutError),
    # anymodbus raises ModbusUnsupportedFunctionError only for a *received*
    # function code it cannot frame. fujilib sends 03, 04, 06 and 10, so a reply
    # carrying another code is line damage, not the analyzer refusing a function.
    ((ChecksumError, FrameError, ModbusUnsupportedFunctionError), FujiFrameError),
    ((BusClosedError, ConnectionLostError), FujiConnectionError),
    ((ModbusConfigurationError,), FujiConfigurationError),
    ((ProtocolError,), FujiProtocolError),
    ((ModbusError,), FujiModbusError),
    (
        (OSError, anyio.BrokenResourceError, anyio.BusyResourceError, anyio.ClosedResourceError),
        FujiConnectionError,
    ),
    ((ValueError,), FujiConfigurationError),
)


def map_modbus_error(exc: BaseException, *, context: ErrorContext) -> FujiError:
    """The fujilib error for ``exc``, carrying ``context``.

    An exception response adds its ``exception_code`` to the context's
    ``extra``. The caller raises the result ``from exc``.

    Raises:
        TypeError: ``exc`` is not one of :data:`MAPPED_EXCEPTIONS`.
    """
    if isinstance(exc, ModbusExceptionResponse):
        context = context.merged(exception_code=exc.exception_code)
    for types, fuji_type in _RULES:
        if isinstance(exc, types):
            return fuji_type(f"{context.command_name or 'modbus'}: {exc}", context=context)
    msg = f"{type(exc).__name__} is not a Modbus or serial error"
    raise TypeError(msg)
