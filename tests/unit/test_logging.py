"""The logger tree: canonical names, and the library never adds handlers."""

from __future__ import annotations

import logging

import fujilib
from fujilib._logging import ROOT, get_logger


def test_root_logger_name() -> None:
    assert ROOT == "fujilib"
    assert get_logger("").name == "fujilib"


def test_child_logger_is_under_the_root() -> None:
    logger = get_logger("protocol.modbus")
    assert logger.name == "fujilib.protocol.modbus"
    assert logger.parent is not None
    assert logger.parent.name.startswith("fujilib")


def test_importing_the_package_adds_no_handlers() -> None:
    assert fujilib is not None
    assert logging.getLogger(ROOT).handlers == []
