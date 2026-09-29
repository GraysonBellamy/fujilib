"""Error hierarchy: the design §9 tree, context round trips, cross-branch MRO."""

from __future__ import annotations

import dataclasses
import re
from enum import StrEnum

import pytest

from fujilib import errors
from fujilib.errors import (
    ErrorContext,
    FujiAnalyzerStateError,
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

#: Direct bases of every class, transcribed from the design §9 tree.
DESIGN_BASES: dict[type[FujiError], tuple[type[Exception], ...]] = {
    FujiError: (Exception,),
    FujiConfigurationError: (FujiError,),
    FujiValidationError: (FujiConfigurationError,),
    FujiConfirmationRequiredError: (FujiConfigurationError,),
    FujiTransportError: (FujiError,),
    FujiTimeoutError: (FujiTransportError,),
    FujiWriteOutcomeUnknownError: (FujiTimeoutError,),
    FujiConnectionError: (FujiTransportError,),
    FujiResyncRequiredError: (FujiTransportError,),
    FujiProtocolError: (FujiError,),
    FujiFrameError: (FujiProtocolError,),
    FujiDecodeError: (FujiProtocolError,),
    FujiVerificationError: (FujiProtocolError,),
    FujiProtocolUnsupportedError: (FujiProtocolError,),
    FujiModbusError: (FujiProtocolError,),
    FujiModbusIllegalFunctionError: (FujiModbusError, FujiProtocolUnsupportedError),
    FujiModbusIllegalDataAddressError: (FujiModbusError, FujiProtocolUnsupportedError),
    FujiModbusIllegalDataValueError: (FujiModbusError,),
    FujiModbusTimeoutError: (FujiModbusError, FujiTimeoutError),
    FujiCapabilityError: (FujiError,),
    FujiFirmwareError: (FujiCapabilityError,),
    FujiAnalyzerStateError: (FujiError,),
    FujiSinkError: (FujiError,),
    FujiSinkDependencyError: (FujiSinkError, FujiConfigurationError),
    FujiSinkSchemaError: (FujiSinkError,),
    FujiSinkWriteError: (FujiSinkError,),
}

ERROR_CLASSES = list(DESIGN_BASES)


def _exported_error_classes() -> set[type[FujiError]]:
    found: set[type[FujiError]] = set()
    for name in errors.__all__:
        obj: object = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, FujiError):
            found.add(obj)
    return found


def test_exported_classes_are_exactly_the_design_tree() -> None:
    assert _exported_error_classes() == set(DESIGN_BASES)


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=lambda c: c.__name__)
def test_bases_match_design(cls: type[FujiError]) -> None:
    assert cls.__bases__ == DESIGN_BASES[cls]


@pytest.mark.parametrize("cls", ERROR_CLASSES[1:], ids=lambda c: c.__name__)
def test_subclasses_add_no_init_or_slots(cls: type[FujiError]) -> None:
    # The MRO caution in errors.py: one shared __init__, no competing __slots__.
    assert "__init__" not in vars(cls)
    assert "__slots__" not in vars(cls)


@pytest.mark.parametrize(
    "cls",
    [c for c in ERROR_CLASSES if not any(c in b for b in DESIGN_BASES.values())],
    ids=lambda c: c.__name__,
)
def test_leaf_classes_document_retrying(cls: type[FujiError]) -> None:
    # Design §9: each class documents whether it is retryable.
    assert cls.__doc__ is not None
    assert re.search(r"retr(y|ied|yable)", cls.__doc__, re.IGNORECASE)


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=lambda c: c.__name__)
def test_with_context_round_trip(cls: type[FujiError]) -> None:
    cause = ValueError("underlying")
    try:
        raise cls("boom", context=ErrorContext(address=1)) from cause
    except FujiError as caught:
        original = caught

    enriched = original.with_context(port="COM8", function_code=0x04, retries=2)

    assert type(enriched) is cls
    assert enriched is not original
    assert enriched.args == ("boom",)
    assert enriched.context.address == 1
    assert enriched.context.port == "COM8"
    assert enriched.context.function_code == 0x04
    assert enriched.context.extra == {"retries": 2}
    assert enriched.__cause__ is cause
    assert enriched.__traceback__ is original.__traceback__
    # The original is untouched.
    assert original.context == ErrorContext(address=1)


@pytest.mark.parametrize(
    ("cls", "also"),
    [
        (FujiModbusTimeoutError, FujiTimeoutError),
        (FujiModbusTimeoutError, FujiTransportError),
        (FujiModbusIllegalFunctionError, FujiProtocolUnsupportedError),
        (FujiModbusIllegalDataAddressError, FujiProtocolUnsupportedError),
        (FujiSinkDependencyError, FujiConfigurationError),
        (FujiWriteOutcomeUnknownError, FujiTimeoutError),
    ],
    ids=lambda c: c.__name__,
)
def test_cross_branch_errors_are_caught_by_both_branches(
    cls: type[FujiError], also: type[FujiError]
) -> None:
    with pytest.raises(also):
        raise cls("x")


def test_base_construction_carries_empty_context() -> None:
    err = FujiError("boom")
    assert str(err) == "boom"
    assert err.context == ErrorContext()


def test_context_renders_into_str() -> None:
    err = FujiModbusTimeoutError(
        "no reply",
        context=ErrorContext(
            command_name="poll",
            protocol="modbus_rtu",
            port="COM8",
            address=1,
            channel="CH3",
            register_address=0x000C,
            function_code=0x04,
            request=bytes.fromhex("01 04 00 0C 00 03 70 08"),
            elapsed_s=0.51234,
            extra={"attempt": 3},
        ),
    )
    rendered = str(err)
    assert rendered.startswith("no reply [")
    for expected in (
        "command=poll",
        "protocol=modbus_rtu",
        "port=COM8",
        "address=1",
        "channel=CH3",
        "register=0x000C",
        "fc=0x04",
        "elapsed_s=0.512",
        "request=01 04 00 0C 00 03 70 08",
        "extra={'attempt': 3}",
    ):
        assert expected in rendered


def test_str_enum_values_render_as_their_value() -> None:
    # ProtocolKind and ChannelId will be StrEnums; the str-typed fields take them as-is.
    class Kind(StrEnum):
        MODBUS_RTU = "modbus_rtu"

    rendered = str(FujiError("x", context=ErrorContext(protocol=Kind.MODBUS_RTU)))
    assert "protocol=modbus_rtu" in rendered


def test_merged_routes_unknown_keys_to_extra() -> None:
    ctx = ErrorContext(port="COM8", extra={"a": 1}).merged(port="COM9", retries=2)
    assert ctx.port == "COM9"
    assert ctx.extra == {"a": 1, "retries": 2}


def test_merged_without_extra_updates_keeps_extra() -> None:
    ctx = ErrorContext(extra={"a": 1})
    assert ctx.merged(address=2).extra is ctx.extra


def test_extra_is_frozen() -> None:
    ctx = ErrorContext(extra={"a": 1})
    with pytest.raises(TypeError):
        ctx.extra["b"] = 2  # type: ignore[index]


def test_extra_is_copied_from_the_callers_mapping() -> None:
    source = {"a": 1}
    ctx = ErrorContext(extra=source)
    source["b"] = 2
    assert ctx.extra == {"a": 1}


def test_context_is_frozen() -> None:
    ctx = ErrorContext(port="COM8")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.port = "COM9"  # type: ignore[misc]
