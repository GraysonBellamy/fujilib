"""fujilib — async Python driver for Fuji Electric ZP-series NDIR gas analyzers.

Covers the ZPA and the ZPB / ZPG / ZPAJ / ZPG3E models that share its MODBUS
map, over MODBUS RTU (RS-485 or RS-232C) at a fixed 38400 8-N-1. It is built on
``anyserial`` and ``anymodbus``.

``fujilib`` is a member of the ``*lib`` instrument-driver family; family harmony
is defined at the boundary (entry point, frozen models, error hierarchy,
streaming/sinks/sync/CLI conventions, tooling and the unified device-library
API). The architecture is described in ``docs/design.md``.
"""

from __future__ import annotations

from fujilib.errors import (
    ErrorContext,
    FujiCapabilityError,
    FujiConfigurationError,
    FujiConfirmationRequiredError,
    FujiConnectionError,
    FujiDecodeError,
    FujiError,
    FujiFirmwareError,
    FujiFrameError,
    FujiModbusError,
    FujiModbusIllegalDataAddressError,
    FujiModbusIllegalDataValueError,
    FujiModbusIllegalFunctionError,
    FujiModbusTimeoutError,
    FujiProtocolError,
    FujiProtocolUnsupportedError,
    FujiResyncRequiredError,
    FujiSinkDependencyError,
    FujiSinkError,
    FujiSinkSchemaError,
    FujiSinkWriteError,
    FujiTimeoutError,
    FujiTransportError,
    FujiValidationError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.version import __version__

__all__ = [
    "ErrorContext",
    "FujiCapabilityError",
    "FujiConfigurationError",
    "FujiConfirmationRequiredError",
    "FujiConnectionError",
    "FujiDecodeError",
    "FujiError",
    "FujiFirmwareError",
    "FujiFrameError",
    "FujiModbusError",
    "FujiModbusIllegalDataAddressError",
    "FujiModbusIllegalDataValueError",
    "FujiModbusIllegalFunctionError",
    "FujiModbusTimeoutError",
    "FujiProtocolError",
    "FujiProtocolUnsupportedError",
    "FujiResyncRequiredError",
    "FujiSinkDependencyError",
    "FujiSinkError",
    "FujiSinkSchemaError",
    "FujiSinkWriteError",
    "FujiTimeoutError",
    "FujiTransportError",
    "FujiValidationError",
    "FujiVerificationError",
    "FujiWriteOutcomeUnknownError",
    "__version__",
]
