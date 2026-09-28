"""Defaults, the reentrant operation lock and operation deadlines (design §2.4, §4.2, §6.4)."""

from __future__ import annotations

import math

import anyio
import anyio.lowlevel
import pytest
from anymodbus import estimate_late_reply_window

from fujilib._deadline import Deadline
from fujilib._lock import maybe_acquire
from fujilib.config import DEFAULTS, Defaults
from fujilib.errors import FujiTimeoutError, FujiValidationError

pytestmark = pytest.mark.anyio


def test_defaults_are_the_designed_values() -> None:
    assert Defaults() == DEFAULTS
    assert DEFAULTS.request_timeout_s == 0.5
    assert DEFAULTS.inter_frame_idle_s == 0.005
    assert DEFAULTS.startup_settle_s == 0.05
    assert DEFAULTS.read_retries == 2
    assert DEFAULTS.resync_window_s == 0.1


def test_resync_window_covers_the_slowest_documented_reply() -> None:
    # 30 ms turnaround, a 64-word reply (133 bytes), the FTDI 16 ms latency timer.
    slowest = estimate_late_reply_window(
        baudrate=38_400, max_turnaround=0.030, max_reply_bytes=133, latency=0.016
    )
    assert DEFAULTS.resync_window_s > slowest


# --- maybe_acquire ------------------------------------------------------------------------


async def test_maybe_acquire_is_reentrant_for_the_holder() -> None:
    lock = anyio.Lock()
    async with maybe_acquire(lock):
        async with maybe_acquire(lock):
            assert lock.locked()
        assert lock.locked()  # the inner exit did not release it
    assert not lock.locked()


async def test_a_child_task_of_the_holder_is_not_an_owner() -> None:
    lock = anyio.Lock()
    order: list[str] = []

    async def child() -> None:
        async with maybe_acquire(lock):
            order.append("child")

    async with anyio.create_task_group() as tg:
        async with maybe_acquire(lock):
            _ = tg.start_soon(child)
            await anyio.sleep(0.01)
            order.append("holder")
    assert order == ["holder", "child"]


async def test_maybe_acquire_waits_for_the_holder() -> None:
    lock = anyio.Lock()
    order: list[str] = []

    async def holder(started: anyio.Event) -> None:
        async with maybe_acquire(lock):
            started.set()
            await anyio.sleep(0.02)
            order.append("holder")

    started = anyio.Event()
    async with anyio.create_task_group() as tg:
        _ = tg.start_soon(holder, started)
        await started.wait()
        async with maybe_acquire(lock):
            order.append("waiter")
    assert order == ["holder", "waiter"]


# --- Deadline -----------------------------------------------------------------------------


async def test_unbounded_deadline() -> None:
    dl = Deadline.after(None, operation="poll")
    assert not dl.bounded
    assert dl.remaining() == math.inf
    assert dl.elapsed() >= 0
    with dl.enforce():
        await anyio.lowlevel.checkpoint()


async def test_bounded_deadline_counts_down() -> None:
    dl = Deadline.after(10.0, operation="poll")
    assert dl.bounded
    assert 9.0 < dl.remaining() <= 10.0


@pytest.mark.parametrize("timeout", [-1.0, math.nan, math.inf])
async def test_bad_timeouts_are_refused(timeout: float) -> None:
    with pytest.raises(FujiValidationError, match="timeout"):
        Deadline.after(timeout, operation="poll")


async def test_enforce_raises_a_timeout_error_with_context() -> None:
    dl = Deadline.after(0.01, operation="poll")
    with pytest.raises(FujiTimeoutError, match="poll did not finish") as info:
        with dl.enforce():
            await anyio.sleep(1)
    assert info.value.context.command_name == "poll"
    assert info.value.context.elapsed_s is not None
    # uvloop's clock counts whole milliseconds, so 10 ms can measure a hair under 0.01 s.
    assert info.value.context.elapsed_s >= 0.01 - 1e-6
    assert dl.remaining() < 0


async def test_enforce_leaves_an_outer_cancellation_alone() -> None:
    dl = Deadline.after(10.0, operation="poll")
    with anyio.move_on_after(0.01) as outer:
        with dl.enforce():
            await anyio.sleep(1)
    assert outer.cancelled_caught


async def test_zero_timeout_expires_at_once() -> None:
    dl = Deadline.after(0, operation="poll")
    with pytest.raises(FujiTimeoutError):
        with dl.enforce():
            await anyio.sleep(0.01)
