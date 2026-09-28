"""Backends for the hardware tests.

Trio cannot read a real Windows COM port with ``anyserial`` 0.1.2: an overlapped read
that waits for data completes with ``STATUS_TIMEOUT``, which trio raises as
``WinError 1460`` and ``anyserial`` treats as a failed port (design §4.7 item 14). The
simulator's port pair does not take that path, so the unit tests pass on trio. Here
trio is an expected failure on Windows, and a strict one: when ``anyserial`` is fixed
the tests pass, and the mark must go.
"""

from __future__ import annotations

import sys

import pytest

from fujilib.errors import FujiConnectionError

_TRIO_ON_WINDOWS = pytest.mark.xfail(
    sys.platform == "win32",
    reason="anyserial 0.1.2: idle receive on a Windows COM port under trio raises WinError 1460",
    raises=FujiConnectionError,
    strict=True,
)


@pytest.fixture(
    params=[
        pytest.param("asyncio", id="asyncio"),
        pytest.param("trio", id="trio", marks=_TRIO_ON_WINDOWS),
    ]
)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    """Real ports: asyncio everywhere, trio where ``anyserial`` supports it."""
    return request.param
