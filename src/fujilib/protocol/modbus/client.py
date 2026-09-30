"""The Modbus client of one station: block reads, counters and write primitives.

A :class:`ModbusClient` is the only holder of an ``anymodbus.Slave`` (design
§5.4). It moves words; it knows nothing about what they mean. Every call:

1. validates its arguments before ``anymodbus`` sees them (design §4.4);
2. takes the port's operation lock (reentrantly), inside the operation
   deadline, so queue time counts against the deadline (design §6.4);
3. refuses, before any I/O, a request the port cannot send in time;
4. calls ``anymodbus``, which checks each reply against the request (its
   function code, its length, a write's echo), retries reads that were lost
   or damaged in transit, and reports every attempt to the port;
5. takes the request's timing and the traffic counters from those reports,
   and translates failures at this single boundary (design §4.6).

**Timing.** ``anymodbus`` reports when each request had been sent (after the
inter-frame gap, the write and the drain) and when its attempt ended. A
:class:`~fujilib.devices.models.TransferTiming` is those two moments, so it
never includes time spent waiting for the line (design §4.2).

**Reads** are retried by ``anymodbus`` when a reply was lost, damaged,
mismatched or of the wrong length (design §4.5). An exception reply is an
answer and is not retried. Each failed attempt is counted by kind; failed
attempts that a later attempt of the same read recovered make up
:attr:`ModbusClient.recoverable_error_count` (unified API §J).

**Writes are never retried.** The two write primitives check the frozen write
envelope as the last step before ``anymodbus``, independently of the registry
(design §5.4). Once a write request may have been sent, every failure except an
exception reply makes its outcome unknown
(:class:`~fujilib.errors.FujiWriteOutcomeUnknownError`, design §6.4): a lost,
damaged or mismatched reply, a port that fails while waiting, or a deadline
that expires. An exception reply is a definite refusal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
import anyio.lowlevel
from anymodbus import (
    BusClosedError,
    ConnectionLostError,
    FrameTimeoutError,
    ModbusExceptionResponse,
    ProtocolError,
    TransactionOutcome,
    UnexpectedResponseError,
)

from fujilib._deadline import Deadline
from fujilib._lock import maybe_acquire
from fujilib.devices.models import TransferTiming
from fujilib.errors import (
    ErrorContext,
    FujiError,
    FujiTimeoutError,
    FujiValidationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.errors import MAPPED_EXCEPTIONS, map_modbus_error
from fujilib.protocol.modbus.read_plan import ZP_MAX_WORDS
from fujilib.registry.regions import (
    FC_READ_HOLDING,
    FC_READ_INPUT,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    RegisterTable,
)
from fujilib.registry.write_policy import check_envelope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from anymodbus import Slave, TransactionInfo

    from fujilib.protocol.modbus.port import ModbusPort
    from fujilib.protocol.modbus.read_plan import BlockRead

__all__ = [
    "BlockReply",
    "ClientCounters",
    "FailureKind",
    "ModbusClient",
    "PlanReply",
]

_WORD_MAX: Final = 0xFFFF
_READ_FUNCTIONS: Final = frozenset({FC_READ_HOLDING, FC_READ_INPUT})


class FailureKind(StrEnum):
    """Why one transaction attempt failed."""

    TIMEOUT = "timeout"
    """No reply within the request timeout."""
    FRAME = "frame"
    """A damaged or malformed reply: a bad CRC, a truncated frame."""
    UNEXPECTED = "unexpected"
    """A well-formed reply that does not answer the request: another function
    code, another word count, or a write echo that differs."""
    EXCEPTION = "exception"
    """A Modbus exception reply."""
    CONNECTION = "connection"
    """The port closed or failed."""
    CANCELLED = "cancelled"
    """The attempt was cancelled, by a deadline or by the caller."""
    OTHER = "other"
    """Anything else ``anymodbus`` raised."""


_OUTCOME_KIND: Final[Mapping[TransactionOutcome, FailureKind]] = MappingProxyType(
    {
        TransactionOutcome.TIMEOUT: FailureKind.TIMEOUT,
        TransactionOutcome.CHECKSUM_ERROR: FailureKind.FRAME,
        TransactionOutcome.FRAME_ERROR: FailureKind.FRAME,
        TransactionOutcome.UNEXPECTED_RESPONSE: FailureKind.UNEXPECTED,
        TransactionOutcome.EXCEPTION_REPLY: FailureKind.EXCEPTION,
        TransactionOutcome.CONNECTION_ERROR: FailureKind.CONNECTION,
        TransactionOutcome.CANCELLED: FailureKind.CANCELLED,
    }
)

_CONNECTION_ERRORS: Final = (
    BusClosedError,
    ConnectionLostError,
    OSError,
    anyio.BrokenResourceError,
    anyio.BusyResourceError,
    anyio.ClosedResourceError,
)


def _is_word(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _WORD_MAX


def _kind(exc: BaseException) -> FailureKind:
    """The kind of a failure ``anymodbus`` raised without reporting an attempt."""
    if isinstance(exc, ModbusExceptionResponse):
        return FailureKind.EXCEPTION
    if isinstance(exc, FrameTimeoutError):
        return FailureKind.TIMEOUT
    if isinstance(exc, UnexpectedResponseError):
        return FailureKind.UNEXPECTED
    if isinstance(exc, ProtocolError):
        return FailureKind.FRAME
    if isinstance(exc, _CONNECTION_ERRORS):
        return FailureKind.CONNECTION
    return FailureKind.OTHER


@dataclass(slots=True)
class ClientCounters:
    """Traffic counters of one station's client. Mutable; read them, don't write them."""

    requests: int = 0
    """Attempts ``anymodbus`` made."""
    retries: int = 0
    """Read attempts that repeated a failed one."""
    recovered: int = 0
    """Failed read attempts that a later attempt of the same read recovered."""
    failures: dict[FailureKind, int] = field(default_factory=dict[FailureKind, int])
    """Failed attempts, by kind."""

    def count_failure(self, kind: FailureKind) -> None:
        """Count one failed attempt."""
        self.failures[kind] = self.failures.get(kind, 0) + 1


@dataclass(frozen=True, slots=True)
class BlockReply:
    """The words one block read returned, and when."""

    block: BlockRead
    words: tuple[int, ...]
    timing: TransferTiming
    attempts: int
    """1 when the first attempt succeeded."""

    @property
    def raw(self) -> bytes:
        """The words as on the wire: big endian, two bytes each."""
        return b"".join(w.to_bytes(2, "big") for w in self.words)

    def to_bank(self) -> dict[int, int]:
        """Each address of the block mapped to its word."""
        return self.block.to_bank(self.words)


@dataclass(frozen=True, slots=True)
class PlanReply:
    """The replies to a read plan, in plan order."""

    replies: tuple[BlockReply, ...]

    @property
    def timings(self) -> tuple[TransferTiming, ...]:
        """The timing of each block."""
        return tuple(r.timing for r in self.replies)

    @property
    def raw(self) -> bytes:
        """Every block's words, concatenated in plan order (the shape of ``Frame.raw``)."""
        return b"".join(r.raw for r in self.replies)

    def bank(self, table: RegisterTable) -> Mapping[int, int]:
        """The words read from ``table``, by address."""
        bank: dict[int, int] = {}
        for reply in self.replies:
            if reply.block.function == table.read_function:
                bank.update(reply.to_bank())
        return MappingProxyType(bank)

    @property
    def input(self) -> Mapping[int, int]:
        """The input-register words read, by address."""
        return self.bank(RegisterTable.INPUT)

    @property
    def holding(self) -> Mapping[int, int]:
        """The holding-register words read, by address."""
        return self.bank(RegisterTable.HOLDING)


@dataclass(frozen=True, slots=True)
class _Failed:
    """A failed call: the fujilib error, why, and the original exception."""

    error: FujiError
    kind: FailureKind
    cause: BaseException | None


class ModbusClient:
    """One station on a :class:`~fujilib.protocol.modbus.port.ModbusPort`.

    Created by :meth:`ModbusPort.client`, never directly.
    """

    def __init__(self, port: ModbusPort, address: int, slave: Slave) -> None:
        """Bind ``slave`` (station ``address``) to ``port``; internal."""
        self._port = port
        self._address = address
        self._slave = slave
        self.counters = ClientCounters()
        """This station's traffic counters."""

    @property
    def address(self) -> int:
        """The station number, 1-31."""
        return self._address

    @property
    def port(self) -> ModbusPort:
        """The port the station is on."""
        return self._port

    @property
    def label(self) -> str:
        """The port's canonical name."""
        return self._port.label

    @property
    def recoverable_error_count(self) -> int:
        """Failed read attempts that a retry recovered (unified API §J)."""
        return self.counters.recovered

    # --- Reads ---------------------------------------------------------------------------

    async def read(
        self, block: BlockRead, *, deadline: Deadline | None = None, command: str = "read"
    ) -> BlockReply:
        """Read one block.

        Raises:
            FujiValidationError: the block is not a read of 1-64 words; nothing was sent.
            FujiModbusError: the analyzer answered with an exception.
            FujiModbusTimeoutError: no reply, after every retry.
            FujiFrameError: damaged or malformed replies, after every retry.
            FujiProtocolError: replies that did not answer the request (another
                function code or word count), after every retry.
            FujiTimeoutError: ``deadline`` expired.
            FujiResyncRequiredError: ``deadline`` ends inside a quiet window.
            FujiConnectionError: the port is closed or failed.
        """
        reply = await self.read_plan((block,), deadline=deadline, command=command)
        return reply.replies[0]

    async def read_plan(
        self,
        plan: Sequence[BlockRead],
        *,
        deadline: Deadline | None = None,
        command: str = "read",
    ) -> PlanReply:
        """Read every block of ``plan``, holding the operation lock throughout.

        A failure after the first block carries the blocks already read in its
        context's ``extra["completed"]``, as ``((fc, address, count), words)``
        pairs (design §4.3).

        Raises:
            FujiError: as :meth:`read`.
        """
        for block in plan:
            self._check_read(block, command)
        dl = deadline if deadline is not None else Deadline.after(None, operation=command)
        replies: list[BlockReply] = []
        try:
            with dl.enforce():
                async with maybe_acquire(self._port.lock):
                    for block in plan:
                        # One at a time: a failure reports the blocks already read.
                        replies.append(await self._read_block(block, dl, command))  # noqa: PERF401
        except FujiError as exc:
            raise self._located(exc, replies) from exc.__cause__
        except anyio.get_cancelled_exc_class():
            # A caller enforcing the same deadline around this call catches its
            # expiry first (the outermost cancelled scope does); report it here,
            # with what was read, rather than as a bare timeout outside.
            if dl.remaining() <= 0:
                raise self._located(dl.expired_error(), replies) from None
            raise
        return PlanReply(tuple(replies))

    def _located(self, exc: FujiError, replies: Sequence[BlockReply]) -> FujiError:
        """``exc`` with the port and station, and the blocks already read, in its context."""
        extra: dict[str, object] = {}
        if replies:
            extra["completed"] = tuple((r.block.key, r.words) for r in replies)
        return exc.with_context(
            protocol=ProtocolKind.MODBUS_RTU, port=self.label, address=self._address, **extra
        )

    def _check_read(self, block: BlockRead, command: str) -> None:
        context = self._context(command, block.function, block.address, block.count)
        if block.function not in _READ_FUNCTIONS:
            msg = f"FC{block.function:02X} is not a register read"
            raise FujiValidationError(msg, context=context)
        if not 1 <= block.count <= ZP_MAX_WORDS:
            msg = f"a read must be 1-{ZP_MAX_WORDS} words, got {block.count}"
            raise FujiValidationError(msg, context=context)
        if block.address < 0 or block.last_address > _WORD_MAX:
            msg = f"a read of {block.count} words at {block.address} leaves the address space"
            raise FujiValidationError(msg, context=context)

    async def _read_block(self, block: BlockRead, dl: Deadline, command: str) -> BlockReply:
        fc, address, count = block.key
        read = (
            self._slave.read_holding_registers
            if fc == FC_READ_HOLDING
            else self._slave.read_input_registers
        )
        context = self._context(command, fc, address, count)
        outcome = await self._transact(lambda: read(address, count=count), dl, context)
        if isinstance(outcome, _Failed):
            raise outcome.error from outcome.cause
        words, timing, attempts = outcome
        return BlockReply(block, words, timing, attempts)

    # --- Writes --------------------------------------------------------------------------

    async def write_register(
        self,
        address: int,
        value: int,
        *,
        deadline: Deadline | None = None,
        command: str = "write_register",
    ) -> TransferTiming:
        """FC06: write one word. Never retried.

        Raises:
            FujiValidationError: a bad argument, or outside the write envelope;
                nothing was sent.
            FujiModbusError: the analyzer refused the write with an exception reply.
            FujiWriteOutcomeUnknownError: the request may have been applied, but
                no valid reply confirmed it.
            FujiTimeoutError: ``deadline`` expired before anything was sent.
            FujiResyncRequiredError: ``deadline`` ends inside a quiet window.
            FujiConnectionError: the port is closed; nothing was sent.
        """
        values = (value,)
        self._check_write(FC_WRITE_SINGLE, address, values, command)
        return await self._write(
            FC_WRITE_SINGLE,
            address,
            values,
            lambda: self._slave.write_register(address, value),
            deadline=deadline,
            command=command,
        )

    async def write_registers(
        self,
        address: int,
        values: Sequence[int],
        *,
        deadline: Deadline | None = None,
        command: str = "write_registers",
    ) -> TransferTiming:
        """FC10: write 1-64 consecutive words. Never retried.

        Raises:
            FujiError: as :meth:`write_register`.
        """
        words = tuple(values)
        self._check_write(FC_WRITE_MULTIPLE, address, words, command)
        return await self._write(
            FC_WRITE_MULTIPLE,
            address,
            words,
            lambda: self._slave.write_registers(address, words),
            deadline=deadline,
            command=command,
        )

    def _check_write(self, fc: int, address: int, values: tuple[int, ...], command: str) -> None:
        context = self._context(command, fc, address, len(values))
        limit = 1 if fc == FC_WRITE_SINGLE else ZP_MAX_WORDS
        if not 1 <= len(values) <= limit:
            msg = f"FC{fc:02X} writes 1-{limit} words, got {len(values)}"
            raise FujiValidationError(msg, context=context)
        for value in values:
            if not _is_word(value):
                msg = f"register values must be integers 0-0xFFFF, got {value!r}"
                raise FujiValidationError(msg, context=context)
        if not (_is_word(address) and _is_word(address + len(values) - 1)):
            msg = f"a write of {len(values)} words at {address!r} leaves the address space"
            raise FujiValidationError(msg, context=context)

    async def _write(
        self,
        fc: int,
        address: int,
        values: tuple[int, ...],
        call: Callable[[], Awaitable[None]],
        *,
        deadline: Deadline | None,
        command: str,
    ) -> TransferTiming:
        dl = deadline if deadline is not None else Deadline.after(None, operation=command)
        context = self._context(command, fc, address, len(values))
        sent = False

        def before_send() -> None:
            nonlocal sent
            # The last check before anymodbus, independent of the registry (design §5.4).
            check_envelope(fc, address, len(values), values=values)
            sent = True

        try:
            with dl.enforce():
                async with maybe_acquire(self._port.lock):
                    outcome = await self._transact(call, dl, context, before_send=before_send)
        except FujiTimeoutError as exc:
            if sent:
                raise self._unknown_outcome(context, str(exc), FailureKind.CANCELLED) from exc
            raise self._located(exc, ()) from exc.__cause__
        except anyio.get_cancelled_exc_class():
            # A caller enforcing the same deadline around this call catches its
            # expiry first; the write may already be on the wire, so say so.
            if sent and dl.remaining() <= 0:
                reason = str(dl.expired_error())
                raise self._unknown_outcome(context, reason, FailureKind.CANCELLED) from None
            raise
        if not isinstance(outcome, _Failed):
            return outcome[1]
        if outcome.kind is FailureKind.EXCEPTION:
            # An exception reply is a definite refusal: the write was not applied.
            raise outcome.error from outcome.cause
        # The request went out and no valid reply came back: lost, damaged,
        # mismatched, or the port failed while waiting. It may have been applied.
        raise self._unknown_outcome(context, str(outcome.error), outcome.kind) from outcome.cause

    @staticmethod
    def _unknown_outcome(
        context: ErrorContext, reason: str, failure: FailureKind
    ) -> FujiWriteOutcomeUnknownError:
        msg = f"the write may or may not have been applied: {reason}"
        return FujiWriteOutcomeUnknownError(
            msg,
            context=context.merged(
                write_state="unknown", transmission_started=True, failure=failure.value
            ),
        )

    # --- One call --------------------------------------------------------------------------

    async def _transact[T](
        self,
        call: Callable[[], Awaitable[T]],
        dl: Deadline,
        context: ErrorContext,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[T, TransferTiming, int] | _Failed:
        """One call to ``anymodbus``, under the operation lock.

        Returns the result, its timing and the number of attempts, or why it failed.
        """
        self._port.check_ready(dl, context=context)
        # Take a pending cancellation (an expired deadline) here, while nothing
        # has been sent, rather than inside anymodbus after ``before_send``.
        await anyio.lowlevel.checkpoint_if_cancelled()
        if before_send is not None:
            before_send()
        with self._port.record_attempts() as attempts:
            try:
                result = await call()
            except MAPPED_EXCEPTIONS as exc:
                return self._failed(exc, context, attempts)
            finally:
                self._tally(attempts)
        return result, _timing(attempts[-1]), len(attempts)

    def _tally(self, attempts: Sequence[TransactionInfo]) -> None:
        for info in attempts:
            self.counters.requests += 1
            if info.outcome is not TransactionOutcome.REPLY:
                self.counters.count_failure(_OUTCOME_KIND.get(info.outcome, FailureKind.OTHER))
            if info.will_retry:
                self.counters.retries += 1
        if attempts and attempts[-1].outcome is TransactionOutcome.REPLY:
            self.counters.recovered += len(attempts) - 1

    def _failed(
        self, exc: BaseException, context: ErrorContext, attempts: Sequence[TransactionInfo]
    ) -> _Failed:
        if attempts:
            last = attempts[-1]
            kind = _OUTCOME_KIND.get(last.outcome, FailureKind.OTHER)
            context = context.merged(
                attempt=len(attempts), elapsed_s=last.ended_at - attempts[0].started_at
            )
        else:
            # Refused before any attempt (the bus was closed): nothing was reported.
            kind = _kind(exc)
            self.counters.count_failure(kind)
        return _Failed(map_modbus_error(exc, context=context), kind, exc)

    def _context(self, command: str, fc: int, address: int, count: int) -> ErrorContext:
        return ErrorContext(
            command_name=command,
            protocol=ProtocolKind.MODBUS_RTU,
            port=self._port.label,
            address=self._address,
            function_code=fc,
            register_address=address,
            extra={"count": count},
        )

    def __repr__(self) -> str:
        return f"<ModbusClient {self.label} station {self._address}>"


def _timing(info: TransactionInfo) -> TransferTiming:
    """A reply's timing from ``anymodbus``'s report: from the request sent to the attempt's end."""
    sent_ns = info.sent_at_ns if info.sent_at_ns is not None else info.ended_at_ns
    now_ns, now = time.monotonic_ns(), datetime.now(UTC)
    return TransferTiming(
        requested_at=now - timedelta(microseconds=(now_ns - sent_ns) / 1000),
        received_at=now - timedelta(microseconds=(now_ns - info.ended_at_ns) / 1000),
        t_request_mono_ns=sent_ns,
        t_reply_mono_ns=info.ended_at_ns,
    )
