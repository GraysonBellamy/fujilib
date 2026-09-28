"""The package imports, exposes its version and keeps a consistent public surface."""

from __future__ import annotations

from importlib.metadata import version

import fujilib
from fujilib import errors


def test_version_is_the_installed_distribution_version() -> None:
    assert isinstance(fujilib.__version__, str)
    assert fujilib.__version__
    assert fujilib.__version__ == version("fujilib")


def test_all_is_sorted_and_unique() -> None:
    assert list(fujilib.__all__) == sorted(set(fujilib.__all__))
    assert list(errors.__all__) == sorted(set(errors.__all__))


def test_every_exported_name_resolves() -> None:
    for name in fujilib.__all__:
        assert hasattr(fujilib, name), name


def test_every_error_is_reexported_at_top_level() -> None:
    for name in errors.__all__:
        assert getattr(fujilib, name) is getattr(errors, name), name
