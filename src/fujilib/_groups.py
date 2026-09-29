"""Exception groups of one exception, unwrapped.

A task group reports even a single failure wrapped in an exception group.
Where fujilib runs a task group on the caller's behalf (the recorder, the
blocking portal), it unwraps a group of one, so the caller catches the
:class:`~fujilib.errors.FujiError` subclass itself.
"""

from __future__ import annotations

from typing import cast

__all__ = ["unwrap"]


def unwrap(exc: BaseException) -> BaseException:
    """``exc`` without exception-group wrappers of a single exception."""
    while isinstance(exc, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", exc)
        if len(group.exceptions) != 1:
            return group
        exc = group.exceptions[0]
    return exc
