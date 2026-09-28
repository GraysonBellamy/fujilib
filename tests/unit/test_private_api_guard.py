"""fujilib uses only the public API of anymodbus and anyserial.

A private name can change in any release; fujilib's pin (``<0.3`` / ``<0.2``)
would not protect against it. The guard reads every source module's syntax
tree, so names in comments and docstrings do not count.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "fujilib"
FOUNDATIONS = ("anymodbus", "anyserial")
#: Private attributes of the foundations that a device library is tempted to reach for.
PRIVATE_ATTRIBUTES = frozenset(
    {
        "_txn",
        "_one_txn",
        "_handle_request",
        "_dispatch",
        "_last_io_monotonic",
        "_backend",
        "_maybe_drain",
        "_maybe_reset_input",
    }
)
MODULES = sorted(SRC.rglob("*.py"))


def private_uses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root, *parts = node.module.split(".")
            if root in FOUNDATIONS:
                if any(p.startswith("_") for p in parts):
                    found.append(f"from {node.module} import ...")
                found += [f"{node.module}.{a.name}" for a in node.names if a.name.startswith("_")]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root, *parts = alias.name.split(".")
                if root in FOUNDATIONS and any(p.startswith("_") for p in parts):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in PRIVATE_ATTRIBUTES:
            found.append(f".{node.attr} (line {node.lineno})")
    return found


def test_modules_were_found() -> None:
    assert len(MODULES) > 30


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(SRC)))
def test_no_private_foundation_api(path: Path) -> None:
    assert private_uses(path) == []


def test_the_guard_catches_what_it_is_for(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from anymodbus._mock.slave import MockSlave\n"
        "from anymodbus.bus import _t35_for_baud\n"
        "import anyserial._windows._win32\n"
        "async def f(bus):\n"
        "    return await bus._txn()\n",
        encoding="utf-8",
    )
    assert len(private_uses(bad)) == 4
