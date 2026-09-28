"""Async tests run on every AnyIO backend in the matrix (asyncio, asyncio+uvloop, trio).

This proves the ``anyio_backend`` parametrization in ``tests/conftest.py`` on
every CI cell before the library has async code of its own.
"""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest

pytestmark = pytest.mark.anyio


async def _append(results: list[int], value: int) -> None:
    await anyio.lowlevel.checkpoint()
    results.append(value)


async def test_task_group_runs_all_tasks() -> None:
    results: list[int] = []
    async with anyio.create_task_group() as tg:
        for value in range(3):
            # anyio >= 4.15 returns a task handle; it is not needed here.
            _ = tg.start_soon(_append, results, value)
    assert sorted(results) == [0, 1, 2]


async def test_cancel_scope_deadline_is_honoured() -> None:
    with anyio.move_on_after(0.01) as scope:
        await anyio.sleep(10)
    assert scope.cancelled_caught
