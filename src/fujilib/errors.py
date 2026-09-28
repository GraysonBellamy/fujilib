"""Typed exception hierarchy for :mod:`fujilib` (design §9).

Every library exception inherits from :class:`FujiError` and carries a
structured :class:`ErrorContext`. The ``message`` is the human-readable
summary; the context is the machine-readable detail (command, protocol, port,
station address, channel, register and function code, request/response bytes,
elapsed time).

The pattern matches the ``*lib`` family: ``ErrorContext`` is a frozen
``dataclass(slots=True)`` whose ``extra`` mapping is always frozen into a
read-only :class:`types.MappingProxyType`, and :meth:`FujiError.with_context`
does a slot-safe copy so an inner layer can raise and an outer layer enrich.

``anymodbus`` exceptions are translated to this hierarchy at a single boundary
in the Modbus client, always with ``raise ... from exc`` (design §4.6).

**Retrying.** Each class states whether retrying the same call can help. The
library itself retries only reads (design §4.5); writes and operation commands
are never retried automatically, because a lost reply does not mean the write
failed (design §6.4).

**MRO caution.** Where a class belongs to two branches (a Modbus timeout is
also a transport timeout; a missing sink extra is also a configuration
problem), the extra base is a plain marker: every class shares the single
:meth:`FujiError.__init__` and none defines ``__slots__``, so the MRO stays
unambiguous and :meth:`~FujiError.with_context` round-trips through it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


_EMPTY_EXTRA: Mapping[str, Any] = MappingProxyType({})


def _empty_extra() -> Mapping[str, Any]:
    return _EMPTY_EXTRA


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Structured context attached to every :class:`FujiError`.

    Fields are best-effort — missing data is ``None`` rather than raising.

    ``protocol`` and ``channel`` are typed as ``str`` so they accept the
    library's ``ProtocolKind`` and ``ChannelId`` string enums as well as plain
    strings. ``register_address`` is the 0-based on-the-wire address, not the
    30001/40001-based register number.

    ``extra`` accepts any ``Mapping`` and is always frozen into a read-only
    :class:`types.MappingProxyType` at construction so the shared empty
    sentinel can never be mutated through ``error.context.extra[k] = v``.
    """

    command_name: str | None = None
    protocol: str | None = None
    port: str | None = None
    address: int | None = None
    channel: str | None = None
    register_address: int | None = None
    function_code: int | None = None
    request: bytes | None = None
    response: bytes | None = None
    elapsed_s: float | None = None
    extra: Mapping[str, Any] = field(default_factory=_empty_extra)

    def __post_init__(self) -> None:
        if not isinstance(self.extra, MappingProxyType):
            object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    def merged(self, **updates: Any) -> Self:
        """Return a new context with ``updates`` overlaid. Unknown keys go to ``extra``."""
        known: dict[str, Any] = {}
        extra_updates: dict[str, Any] = {}
        for key, value in updates.items():
            if key in _CONTEXT_KNOWN_FIELDS:
                known[key] = value
            else:
                extra_updates[key] = value

        new_extra: Mapping[str, Any] = (
            MappingProxyType({**self.extra, **extra_updates}) if extra_updates else self.extra
        )
        return replace(self, **known, extra=new_extra)


_CONTEXT_KNOWN_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(ErrorContext) if f.name != "extra"
)


_EMPTY_CONTEXT = ErrorContext()


def _format_hex2(value: Any) -> str:
    return f"0x{value:02X}"


def _format_hex4(value: Any) -> str:
    return f"0x{value:04X}"


def _format_elapsed(value: Any) -> str:
    return f"{value:.3f}"


def _format_frame(value: Any) -> str:
    # Space-separated upper-case hex, as in the arrow fixtures (design §2.10).
    return bytes(value).hex(" ").upper()


def _context_bits(ctx: ErrorContext) -> list[str]:
    candidates: tuple[tuple[str, object, Callable[[Any], str]], ...] = (
        ("command", ctx.command_name, str),
        ("protocol", ctx.protocol, str),
        ("port", ctx.port, str),
        ("address", ctx.address, str),
        ("channel", ctx.channel, str),
        ("register", ctx.register_address, _format_hex4),
        ("fc", ctx.function_code, _format_hex2),
        ("elapsed_s", ctx.elapsed_s, _format_elapsed),
        ("request", ctx.request, _format_frame),
        ("response", ctx.response, _format_frame),
    )
    bits = [f"{label}={fmt(value)}" for label, value, fmt in candidates if value is not None]
    if ctx.extra:
        bits.append(f"extra={dict(ctx.extra)!r}")
    return bits


class FujiError(Exception):
    """Base class for every :mod:`fujilib` exception.

    Carries a typed :class:`ErrorContext`. The ``message`` is the human-readable
    summary; the context is the machine-readable detail.
    """

    context: ErrorContext

    def __init__(self, message: str = "", *, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.context = context if context is not None else _EMPTY_CONTEXT

    def with_context(self, **updates: Any) -> Self:
        """Return a copy of this error with its context updated.

        Useful when an inner layer raises and an outer layer wants to enrich
        the context (for instance adding ``port`` or ``elapsed_s``).
        """
        cls = type(self)
        new = cls.__new__(cls)
        new.args = self.args
        try:
            new.__dict__.update(self.__dict__)
        except AttributeError:  # pragma: no cover — no slotted subclass today
            for slot in getattr(cls, "__slots__", ()):
                if hasattr(self, slot):
                    object.__setattr__(new, slot, getattr(self, slot))
        new.context = self.context.merged(**updates)
        new.__cause__ = self.__cause__
        new.__context__ = self.__context__
        new.__traceback__ = self.__traceback__
        return new

    def __str__(self) -> str:
        base = super().__str__()
        bits = _context_bits(self.context)
        return f"{base} [{', '.join(bits)}]" if bits else base


# --- Configuration -------------------------------------------------------


class FujiConfigurationError(FujiError):
    """Configuration-level error: bad arguments or conflicting settings.

    Not retryable: the call itself has to change. A ``ValueError`` escaping
    ``anymodbus`` is a fujilib bug and also surfaces here (design §4.4).
    """


class FujiValidationError(FujiConfigurationError):
    """A request failed validation before any I/O.

    Examples: an unknown parameter name, a channel or range that does not
    exist, a value outside its raw limits. Not retryable.
    """


class FujiConfirmationRequiredError(FujiConfigurationError):
    """An operation above ``SafetyTier.READ_ONLY`` was attempted without ``confirm=True``.

    Raised before any I/O (design §6.1). Retry only by passing
    ``confirm=True`` deliberately.
    """


# --- Transport -----------------------------------------------------------


class FujiTransportError(FujiError):
    """I/O-layer error from the serial transport or the Modbus bus."""


class FujiTimeoutError(FujiTransportError):
    """A transaction timed out, or a per-call operation deadline expired.

    For a deadline, the context carries the operation name and the elapsed
    time (design §4.6, §6.4). Reads may be retried. A write that timed out
    raises :class:`FujiWriteOutcomeUnknownError` instead.
    """


class FujiWriteOutcomeUnknownError(FujiTimeoutError):
    """A write request was sent but its outcome could not be established.

    The analyzer may have applied the write even though the reply was lost
    (design §6.4). Not retryable: read the setting back and decide. The
    context's ``extra`` records the ``write_state``, whether transmission
    started, and any observed value.
    """


class FujiConnectionError(FujiTransportError):
    """The port could not be opened, or the connection was lost.

    Retryable only after the device is reopened.
    """


class FujiResyncRequiredError(FujiTransportError):
    """The port is still discarding a possible late reply after a cancellation.

    New requests are refused until resynchronization completes (design §4.2);
    retry after it has.
    """


# --- Protocol ------------------------------------------------------------


class FujiProtocolError(FujiError):
    """Protocol-level error: framing, decoding, or an unexpected reply."""


class FujiFrameError(FujiProtocolError):
    """Bad CRC, wrong length or malformed framing.

    Usually link noise. Reads are retried by the client; writes are not.
    """


class FujiDecodeError(FujiProtocolError):
    """A register value was received intact but does not decode.

    Examples: a BCD digit above 9, an enum value the manual does not define.
    Not retryable. Distinct from :class:`FujiConfigurationError` (design §4.4).
    """


class FujiVerificationError(FujiProtocolError):
    """The read-back after a write did not match the value written.

    The context carries the expected and observed values. Not retryable: the
    front panel may have changed the setting in between (design §6.3).
    """


class FujiProtocolUnsupportedError(FujiProtocolError):
    """The analyzer does not support this request at all. Not retryable."""


# --- Modbus --------------------------------------------------------------


class FujiModbusError(FujiProtocolError):
    """A Modbus exception response or other Modbus-layer failure.

    ``__cause__`` preserves the original ``anymodbus`` exception.
    """


class FujiModbusIllegalFunctionError(FujiModbusError, FujiProtocolUnsupportedError):
    """Modbus exception 01 — the function code is not implemented.

    The bench unit answers FC01 and FC02 with exception 02 instead
    (design §2.2). Not retryable.
    """


class FujiModbusIllegalDataAddressError(FujiModbusError, FujiProtocolUnsupportedError):
    """Modbus exception 02 — the address is outside the analyzer's map.

    For a well-formed capability probe this marks the capability
    ``UNSUPPORTED`` (design §6.6). Not retryable.
    """


class FujiModbusIllegalDataValueError(FujiModbusError):
    """Modbus exception 03 — illegal data value.

    The manual describes it as "too many words requested / outside the map";
    the bench unit also returns it for a read that crosses a region end
    (design §2.2). Not retryable.
    """


class FujiModbusTimeoutError(FujiModbusError, FujiTimeoutError):
    """No reply arrived within the request timeout.

    A bad CRC, a wrong station number or an over-long inter-byte gap all
    produce no reply (design §2.2). Reads are retried by the client and each
    retry is counted (design §4.5); writes are not.
    """


# --- Capability ----------------------------------------------------------


class FujiCapabilityError(FujiError):
    """The operation is not available on this analyzer, model or option.

    Raised before any I/O once a capability is known to be ``UNSUPPORTED``
    (design §6.6). Not retryable until ``reprobe()``.
    """


class FujiFirmwareError(FujiCapabilityError):
    """The operation needs firmware the analyzer does not have.

    For example, the calibration log needs firmware 2.24 or later. Not
    retryable.
    """


# --- Sinks ---------------------------------------------------------------


class FujiSinkError(FujiError):
    """Base class for errors raised by sinks."""


class FujiSinkDependencyError(FujiSinkError, FujiConfigurationError):
    """A sink's optional backing library is not installed.

    Also a :class:`FujiConfigurationError`, because a missing extra is a
    configuration problem from the caller's point of view. Not retryable until
    the extra is installed.
    """


class FujiSinkSchemaError(FujiSinkError):
    """A batch's shape is incompatible with the sink's locked schema. Not retryable."""


class FujiSinkWriteError(FujiSinkError):
    """The backing store rejected a write.

    ``__cause__`` preserves the backend's exception; whether a retry can help
    depends on it.
    """


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
]
