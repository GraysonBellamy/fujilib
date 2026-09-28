"""Translation of anymodbus and anyserial exceptions (design §4.6)."""

from __future__ import annotations

import anyio
import anymodbus
import anyserial
import pytest

from fujilib.errors import (
    ErrorContext,
    FujiConfigurationError,
    FujiConnectionError,
    FujiFrameError,
    FujiModbusError,
    FujiModbusIllegalDataAddressError,
    FujiModbusIllegalDataValueError,
    FujiModbusIllegalFunctionError,
    FujiModbusTimeoutError,
    FujiProtocolError,
    FujiProtocolUnsupportedError,
    FujiTimeoutError,
)
from fujilib.protocol.modbus.errors import map_modbus_error

CONTEXT = ErrorContext(command_name="poll", port="COM8", address=1, function_code=4)


def response(cls: type[anymodbus.ModbusExceptionResponse], code: int) -> BaseException:
    return cls(function_code=4, exception_code=code)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (response(anymodbus.IllegalFunctionError, 1), FujiModbusIllegalFunctionError),
        # A reply whose function code anymodbus cannot frame: line damage, not a refusal.
        (anymodbus.ModbusUnsupportedFunctionError("fc 0x07"), FujiFrameError),
        (response(anymodbus.IllegalDataAddressError, 2), FujiModbusIllegalDataAddressError),
        (response(anymodbus.IllegalDataValueError, 3), FujiModbusIllegalDataValueError),
        (response(anymodbus.SlaveDeviceFailureError, 4), FujiModbusError),
        (response(anymodbus.SlaveDeviceBusyError, 6), FujiModbusError),
        (response(anymodbus.ModbusUnknownExceptionError, 7), FujiModbusError),
        (anymodbus.FrameTimeoutError("silent"), FujiModbusTimeoutError),
        (anymodbus.CRCError("crc"), FujiFrameError),
        (anymodbus.ChecksumError("checksum"), FujiFrameError),
        (anymodbus.FrameError("truncated"), FujiFrameError),
        (anymodbus.BusClosedError("closed"), FujiConnectionError),
        (anymodbus.ConnectionLostError("unplugged"), FujiConnectionError),
        (anymodbus.ConfigurationError("bad address"), FujiConfigurationError),
        (anymodbus.UnexpectedResponseError("fc 3, expected 4"), FujiProtocolError),
        (anymodbus.ProtocolError("fc 0"), FujiProtocolError),
        (anymodbus.ModbusError("other"), FujiModbusError),
        (anyserial.SerialError("device gone"), FujiConnectionError),
        (OSError("io"), FujiConnectionError),
        (anyio.BrokenResourceError(), FujiConnectionError),
        (anyio.ClosedResourceError(), FujiConnectionError),
        (anyio.BusyResourceError("receiving"), FujiConnectionError),
        (ValueError("register value out of range"), FujiConfigurationError),
    ],
)
def test_each_exception_maps_to_its_fujilib_class(
    exc: BaseException, expected: type[Exception]
) -> None:
    mapped = map_modbus_error(exc, context=CONTEXT)
    assert type(mapped) is expected
    assert mapped.context.port == "COM8"
    assert mapped.context.command_name == "poll"
    assert str(exc) in str(mapped)


def test_the_exception_code_goes_into_the_context() -> None:
    mapped = map_modbus_error(response(anymodbus.IllegalDataAddressError, 2), context=CONTEXT)
    assert mapped.context.extra["exception_code"] == 2
    assert isinstance(mapped, FujiProtocolUnsupportedError)


def test_a_modbus_timeout_is_also_a_transport_timeout() -> None:
    mapped = map_modbus_error(anymodbus.FrameTimeoutError("silent"), context=CONTEXT)
    assert isinstance(mapped, FujiTimeoutError)


def test_a_timeout_is_not_mistaken_for_a_connection_error() -> None:
    # FrameTimeoutError is a TimeoutError, which is an OSError.
    mapped = map_modbus_error(anymodbus.FrameTimeoutError("silent"), context=CONTEXT)
    assert not isinstance(mapped, FujiConnectionError)


def test_a_context_without_a_command_still_reads() -> None:
    mapped = map_modbus_error(anymodbus.CRCError("crc"), context=ErrorContext())
    assert str(mapped).startswith("modbus: crc")


def test_other_exceptions_are_not_translated() -> None:
    with pytest.raises(TypeError, match="RuntimeError"):
        map_modbus_error(RuntimeError("bug"), context=CONTEXT)
