"""Package version, read from the installed distribution metadata.

The version itself is derived from git tags by ``hatch-vcs`` at build time.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("fujilib")
except PackageNotFoundError:  # pragma: no cover - editable / uninstalled
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
