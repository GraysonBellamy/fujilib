"""Operation deadlines (design §6.4).

A per-call ``timeout=`` is a deadline for the **whole operation**: it starts
before the operation lock is acquired and covers queue time, retries, scaling
reads and verification. Nested steps receive the same :class:`Deadline`, so
the budget is shared rather than restarted.

Deadlines run on the AnyIO clock (:func:`anyio.current_time`), the clock
``anymodbus`` measures its inter-frame gap with. They are unrelated to the
wall-clock and monotonic timestamps stored in
:class:`~fujilib.devices.models.TransferTiming`.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from fujilib.errors import ErrorContext, FujiTimeoutError, FujiValidationError

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["Deadline"]


@dataclass(frozen=True, slots=True)
class Deadline:
    """When an operation must be finished, on the AnyIO clock."""

    operation: str
    """The operation's name, for error context."""
    started: float
    """When the operation started."""
    expires: float
    """When it must be finished; ``math.inf`` for no deadline."""

    @classmethod
    def after(cls, timeout: float | None, *, operation: str) -> Deadline:
        """A deadline ``timeout`` seconds from now; ``None`` means no deadline.

        Raises:
            FujiValidationError: ``timeout`` is negative or not finite.
        """
        now = anyio.current_time()
        if timeout is None:
            return cls(operation, now, math.inf)
        if not math.isfinite(timeout) or timeout < 0:
            msg = f"timeout must be a finite, non-negative number of seconds, got {timeout!r}"
            raise FujiValidationError(msg, context=ErrorContext(command_name=operation))
        return cls(operation, now, now + timeout)

    @property
    def bounded(self) -> bool:
        """Whether the operation has a deadline at all."""
        return math.isfinite(self.expires)

    def remaining(self) -> float:
        """Seconds left, ``math.inf`` without a deadline; zero or negative once expired."""
        return self.expires - anyio.current_time()

    def elapsed(self) -> float:
        """Seconds since the operation started."""
        return anyio.current_time() - self.started

    def expired_error(self) -> FujiTimeoutError:
        """The error for an operation that ran out of time."""
        elapsed = self.elapsed()
        msg = f"{self.operation} did not finish within its deadline ({elapsed:.3f} s elapsed)"
        return FujiTimeoutError(
            msg, context=ErrorContext(command_name=self.operation, elapsed_s=elapsed)
        )

    @contextmanager
    def enforce(self) -> Generator[None]:
        """Cancel the enclosed block at the deadline and raise :class:`FujiTimeoutError`.

        Only this deadline's own cancellation is converted; an outer cancel
        scope's cancellation propagates unchanged.
        """
        with anyio.CancelScope(deadline=self.expires) as scope:
            yield
        if scope.cancelled_caught:
            raise self.expired_error()
