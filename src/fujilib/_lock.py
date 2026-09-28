"""Reentrant-safe acquisition of a port's operation lock (design §4.2).

:class:`anyio.Lock` is not reentrant: a second ``async with lock`` from the
task that holds it deadlocks. :func:`maybe_acquire` checks
``lock.statistics().owner`` and skips acquisition when the current task
already holds the lock.

This lets a caller hold a port's operation lock across a batch (the two
blocks of a poll, a recorder tick, a write and its read-back) while every
single transaction underneath still asks for it. Atomicity follows from the
call graph rather than from flags threaded through every layer.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["maybe_acquire"]


@asynccontextmanager
async def maybe_acquire(lock: anyio.Lock) -> AsyncGenerator[None]:
    """Acquire ``lock`` unless the current task already holds it.

    Owner identity is compared with ``==`` because AnyIO's asyncio backend
    returns a fresh ``TaskInfo`` from each :func:`anyio.get_current_task`
    call; equality on the task id carries the identity.
    """
    owner = lock.statistics().owner
    if owner is not None and owner == anyio.get_current_task():
        yield
        return
    async with lock:
        yield
