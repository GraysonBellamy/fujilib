"""Shared helpers for the ``fuji-*`` command-line tools."""

from __future__ import annotations

import json
import sys
from enum import Enum
from typing import TYPE_CHECKING, cast

from fujilib.errors import FujiError

if TYPE_CHECKING:
    from collections.abc import Callable, Sized

__all__ = ["render", "run_cli"]


def run_cli(body: Callable[[], int]) -> int:
    """Run a CLI body, turning a :class:`~fujilib.errors.FujiError` into exit code 1."""
    try:
        return body()
    except FujiError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex(" ").upper()
    return str(value)


def _is_nested(value: object) -> bool:
    return isinstance(value, dict | list) and len(cast("Sized", value)) > 0


def _text(value: object, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            if _is_nested(item):
                out.append(f"{pad}{key}:")
                _text(item, indent + 1, out)
            else:
                out.append(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            if _is_nested(item):
                out.append(f"{pad}-")
                _text(item, indent + 1, out)
            else:
                out.append(f"{pad}- {_scalar(item)}")
    else:
        out.append(f"{pad}{_scalar(value)}")


def _scalar(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Enum):
        return str(value.value)
    if _is_empty_container(value):
        return "none"
    return str(value)


def _is_empty_container(value: object) -> bool:
    return isinstance(value, dict | list) and len(cast("Sized", value)) == 0


def render(report: object, fmt: str) -> str:
    """Render a report as indented text or as JSON."""
    if fmt == "json":
        return json.dumps(report, indent=2, default=_json_default) + "\n"
    out: list[str] = []
    _text(report, 0, out)
    return "\n".join(out) + "\n"
