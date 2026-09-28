"""Gates and bench settings for the hardware tests (design §10).

Each marker runs only when its environment variable is ``1``, and only with a
port named in ``FUJILIB_HARDWARE_PORT``; ``pyproject.toml`` also deselects
them all by default. Each tier needs the owner's authorization before it is
ever run::

    FUJILIB_ENABLE_HARDWARE_TESTS=1 FUJILIB_HARDWARE_PORT=COM8 \\
        uv run pytest -m hardware tests/hardware

``FUJILIB_HARDWARE_ADDRESS`` sets the station (default 1). The markers are
read with ``iter_markers``: ``item.keywords`` would also match the directory
name ``hardware``.
"""

from __future__ import annotations

import os
from typing import Final

import pytest

#: Each marker and the variable that enables it.
GATES: Final = {
    "hardware": "FUJILIB_ENABLE_HARDWARE_TESTS",
    "hardware_stateful": "FUJILIB_ENABLE_STATEFUL_TESTS",
    "hardware_destructive": "FUJILIB_ENABLE_DESTRUCTIVE_TESTS",
}
PORT: Final = os.environ.get("FUJILIB_HARDWARE_PORT", "")
ADDRESS: Final = int(os.environ.get("FUJILIB_HARDWARE_ADDRESS", "1"))


def pytest_runtest_setup(item: pytest.Item) -> None:
    for marker, variable in GATES.items():
        if any(item.iter_markers(marker)) and os.environ.get(variable) != "1":
            pytest.skip(f"set {variable}=1 to run {marker} tests")
    if not PORT:
        pytest.skip("set FUJILIB_HARDWARE_PORT to the analyzer's serial port")


@pytest.fixture
def hardware_port() -> str:
    """The analyzer's serial port."""
    return PORT


@pytest.fixture
def hardware_address() -> int:
    """The analyzer's station number."""
    return ADDRESS


@pytest.fixture
def other_address() -> int:
    """A station number next to the analyzer's, expected to be empty on the bench line."""
    return ADDRESS % 31 + 1
