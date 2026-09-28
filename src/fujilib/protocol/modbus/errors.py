"""Translation of ``anymodbus`` and ``anyserial`` exceptions to :mod:`fujilib.errors` (design §4.6).

Applied at a single boundary, in the Modbus client, always as
``raise mapped from exc`` so the original exception stays reachable.

Order matters. ``anymodbus``'s ``FrameTimeoutError`` is a ``TimeoutError``, and
so an ``OSError``, and its ``TransportError`` (a failing port) is an ``OSError``
too, so the Modbus classes are matched before the built-in ones. ``anymodbus``
translates every stream failure into a ``ModbusError``; the ``anyio`` and
``OSError`` row remains for a failure outside a transaction.

Anything else is not translated: an exception of another kind, such as a bare
``ValueError``, is a bug and propagates as it is.
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
    ((ChecksumError, FrameError), FujiFrameError),
    # TransportError, a port that fails mid-transaction, is a ConnectionLostError.
    ((BusClosedError, ConnectionLostError), FujiConnectionError),
    ((ModbusConfigurationError,), FujiConfigurationError),
    ((ProtocolError,), FujiProtocolError),
    ((ModbusError,), FujiModbusError),
    (
        (OSError, anyio.BrokenResourceError, anyio.BusyResourceError, anyio.ClosedResourceError),
        FujiConnectionError,
    ),
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
