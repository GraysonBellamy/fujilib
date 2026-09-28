"""Shared pytest configuration.

AnyIO's own pytest plugin drives async tests. The ``anyio_backend`` fixture
parametrizes tests across asyncio, asyncio+uvloop when uvloop is available,
and trio. Matches the backend matrix used by the sibling libraries and
``anyserial`` and catches regressions that only surface under one
scheduler.
"""

from __future__ import annotations

import sys
from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.mark.structures import ParameterSet
    from _pytest.python_api import ApproxBase


def approx(
    expected: object,
    *,
    rel: float | None = None,
    abs: float | None = None,  # noqa: A002 - mirrors pytest.approx's parameter name
    nan_ok: bool = False,
) -> ApproxBase:
    """Typed shim over :func:`pytest.approx`.

    ``pytest.approx`` has untyped parameters, so every call site trips pyright's
    strict ``reportUnknownMemberType``. Funnelling through this typed wrapper
    confines that to a single suppressed line here.
    """
    return pytest.approx(expected, rel=rel, abs=abs, nan_ok=nan_ok)  # pyright: ignore[reportUnknownMemberType]


_UVLOOP_UNAVAILABLE = sys.platform == "win32" or find_spec("uvloop") is None

_PARAMS: list[ParameterSet] = [
    pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
    pytest.param(
        ("asyncio", {"use_uvloop": True}),
        id="asyncio+uvloop",
        marks=pytest.mark.skipif(
            _UVLOOP_UNAVAILABLE,
            reason="uvloop is unsupported or not installed on this platform",
        ),
    ),
    pytest.param("trio", id="trio"),
]


@pytest.fixture(params=_PARAMS)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    """Run async tests against asyncio, asyncio+uvloop when available, and trio."""
    return request.param
