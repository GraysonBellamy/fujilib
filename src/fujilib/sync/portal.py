"""The blocking portal behind the sync facade (design §7.3).

:class:`SyncPortal` runs an AnyIO event loop in a background thread
(:func:`anyio.from_thread.start_blocking_portal`) and calls coroutines on it
from ordinary code. A portal is used once: its ``with`` block starts the loop
and ends it.

A single exception raised inside a task group reaches AnyIO wrapped in an
exception group; :meth:`SyncPortal.call` unwraps a group of one, so callers
catch the :class:`~fujilib.errors.FujiError` subclass itself.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Self

from anyio.from_thread import start_blocking_portal

from fujilib._groups import unwrap

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from concurrent.futures import Future
    from contextlib import AbstractAsyncContextManager, AbstractContextManager
    from types import TracebackType

    from anyio.from_thread import BlockingPortal

__all__ = ["SyncPortal"]


class SyncPortal:
    """An event loop in a background thread, for blocking calls into the async core.

    Example::

        with SyncPortal() as portal:
            analyzer = portal.call(open_device, "COM8")
    """

    def __init__(self, *, backend: str = "asyncio") -> None:
        """Prepare a portal on AnyIO ``backend`` (``"asyncio"`` or ``"trio"``)."""
        self._backend_name = backend
        self._cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._entered = False

    @property
    def running(self) -> bool:
        """Whether the portal's loop is running."""
        return self._portal is not None

    def __enter__(self) -> Self:
        """Start the loop.

        Raises:
            RuntimeError: the portal was used before.
        """
        if self._entered:
            msg = "a SyncPortal cannot be used again after it has been closed"
            raise RuntimeError(msg)
        self._entered = True
        cm = start_blocking_portal(self._backend_name)
        self._portal = cm.__enter__()
        self._cm = cm
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the loop and join its thread."""
        cm, self._cm, self._portal = self._cm, None, None
        if cm is not None:
            cm.__exit__(exc_type, exc, tb)

    def _running(self) -> BlockingPortal:
        if self._portal is None:
            msg = "the SyncPortal is not running"
            raise RuntimeError(msg)
        return self._portal

    def call[**P, T](self, func: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> T:
        """Run ``func(*args, **kwargs)`` on the portal's loop and wait for the result.

        Raises:
            RuntimeError: the portal is not running.
        """
        portal = self._running()
        try:
            return portal.call(partial(func, *args, **kwargs))
        except BaseExceptionGroup as group:
            unwrapped = unwrap(group)
            if unwrapped is group:
                raise
        # Raised outside the handler: the group is hidden, and the error keeps
        # its own cause and context (errors.py).
        raise unwrapped

    def start_task_soon[T](self, func: Callable[[], Awaitable[T]]) -> Future[T]:
        """Start ``func()`` on the portal's loop; its future cancels the task when cancelled.

        Raises:
            RuntimeError: the portal is not running.
        """
        return self._running().start_task_soon(func)

    def run_in_loop[T](self, func: Callable[[], T]) -> T:
        """Run the plain function ``func`` in the loop's thread and return its result.

        Raises:
            RuntimeError: the portal is not running.
        """
        return self._running().call(func)

    def wrap_async_context_manager[T](
        self, acm: AbstractAsyncContextManager[T]
    ) -> AbstractContextManager[T]:
        """Present an async context manager, entered and exited on the portal's loop, as a sync one.

        Raises:
            RuntimeError: the portal is not running.
        """
        return self._running().wrap_async_context_manager(acm)
