"""Blocking sink I/O in a worker thread."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from fujilib._groups import unwrap

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["in_thread"]


async def in_thread[T](func: Callable[[], T]) -> T:
    """Run ``func`` in a worker thread; return only once it has finished.

    A write that has started is finished, even when the caller is cancelled,
    so a cancelled writer never leaves half a batch behind and never overlaps
    the sink's next write; the cancellation takes effect once it returns. What
    ``func`` raises is raised as itself.

    The thread runs in a child task: a task group waits for its children even
    when its host is cancelled natively (as asyncio's runner does on Ctrl-C),
    which a shielded scope alone does not survive.
    """
    results: list[T] = []

    async def run() -> None:
        with anyio.CancelScope(shield=True):
            results.append(await anyio.to_thread.run_sync(func))

    failure: BaseException | None = None
    try:
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(run)
    except BaseExceptionGroup as group:
        failure = unwrap(group)  # one child, so one exception
    if failure is not None:
        raise failure
    return results[0]
