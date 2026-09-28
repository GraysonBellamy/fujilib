r"""Canonical serial-port names (design §4.1).

One physical port can be named several ways: ``COM8``, ``com8`` and
``\\.\COM8`` on Windows, or a symlink such as ``/dev/serial/by-id/...`` on
POSIX. The canonical name is the key that decides whether two opens refer to
the same port, and the name fujilib reports in errors and rows.

On Windows the name is also what fujilib passes to ``anyserial``, which adds
the ``\\.\`` device prefix itself; stripping any prefix first avoids
``anyserial`` doubling an existing ``\\?\`` one.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fujilib.errors import ErrorContext, FujiValidationError

__all__ = ["canonical_port"]

_WINDOWS_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\")


def canonical_port(name: str, *, platform: str = sys.platform) -> str:
    r"""The canonical form of serial-port ``name``.

    - Windows: any ``\\.\`` or ``\\?\`` prefix removed, upper-cased
      (device names are case-insensitive), so ``com8`` becomes ``COM8``.
    - Elsewhere: the resolved path when ``name`` exists, so a symlink and its
      target agree; otherwise ``name`` unchanged.

    Args:
        name: The port name as the caller gave it.
        platform: The value of :data:`sys.platform` to canonicalize for.

    Raises:
        FujiValidationError: ``name`` is empty.
    """
    stripped = name.strip()
    if not stripped:
        msg = "the serial port name is empty"
        raise FujiValidationError(msg, context=ErrorContext(port=name))
    if platform == "win32":
        for prefix in _WINDOWS_DEVICE_PREFIXES:
            stripped = stripped.removeprefix(prefix)
        return stripped.upper()
    path = Path(stripped)
    if path.exists():
        return str(path.resolve())
    return stripped
