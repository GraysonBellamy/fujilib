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

from fujilib.devices.capability import Availability, Capability, SafetyTier
from fujilib.devices.models import (
    AnalyzerMetadata,
    AnalyzerStatus,
    ChannelInfo,
    ChannelStatus,
    DeviceInfo,
    Frame,
    RangeInfo,
    Reading,
    ReadingState,
    TransferTiming,
)
from fujilib.devices.snapshot import DeviceSnapshot, FujiDeviceSnapshot
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
from fujilib.protocol.base import ProtocolKind
from fujilib.registry.channels import ChannelId, ChannelRole, Gas, LabelSource
from fujilib.registry.units import Unit
from fujilib.sinks.base import sample_to_row
from fujilib.streaming.sample import Sample
from fujilib.transport.base import SerialSettings
from fujilib.units import to_pint
from fujilib.version import __version__

__all__ = [
    "AnalyzerMetadata",
    "AnalyzerStatus",
    "Availability",
    "Capability",
    "ChannelId",
    "ChannelInfo",
    "ChannelRole",
    "ChannelStatus",
    "DeviceInfo",
    "DeviceSnapshot",
    "ErrorContext",
    "Frame",
    "FujiCapabilityError",
    "FujiConfigurationError",
    "FujiConfirmationRequiredError",
    "FujiConnectionError",
    "FujiDecodeError",
    "FujiDeviceSnapshot",
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
    "Gas",
    "LabelSource",
    "ProtocolKind",
    "RangeInfo",
    "Reading",
    "ReadingState",
    "SafetyTier",
    "Sample",
    "SerialSettings",
    "TransferTiming",
    "Unit",
    "__version__",
    "sample_to_row",
    "to_pint",
]
