"""Every async method has a blocking twin with the same parameters (design §7.3).

For each pair the parameter names, kinds and defaults must match; a sync
wrapper may add only a ``portal`` to the entry points, and nothing to the
analyzer's methods. Every public coroutine of :class:`Analyzer` must have a
twin, and every public property of it one of the same name, so a method added
to one side without the other fails here. Return and parameter annotations
are not compared: turning ``Awaitable[T]`` into ``T`` is the point of the
sync layer.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest

from fujilib import find_devices, open_device
from fujilib.devices.analyzer import Analyzer
from fujilib.sync import Fuji, SyncAnalyzer
from fujilib.sync import find_devices as sync_find_devices

if TYPE_CHECKING:
    from collections.abc import Callable


def _parameters(func: Callable[..., object]) -> list[inspect.Parameter]:
    params = list(inspect.signature(func).parameters.values())
    return params[1:] if params and params[0].name in {"self", "cls"} else params


def assert_parity(
    async_func: Callable[..., object],
    sync_func: Callable[..., object],
    *,
    extra: frozenset[str] = frozenset(),
) -> None:
    sync_params = {p.name: p for p in _parameters(sync_func)}
    async_params = _parameters(async_func)
    for param in async_params:
        assert param.name in sync_params, f"missing parameter {param.name!r}"
        twin = sync_params[param.name]
        assert twin.kind == param.kind, f"{param.name!r}: {twin.kind} is not {param.kind}"
        assert twin.default == param.default, f"{param.name!r}: default differs"
    added = set(sync_params) - {p.name for p in async_params}
    assert added <= extra, f"unexpected sync-only parameters {sorted(added - extra)}"


def _coroutines(cls: type) -> list[str]:
    return sorted(
        name
        for name in dir(cls)
        if not name.startswith("_")
        and inspect.iscoroutinefunction(inspect.getattr_static(cls, name))
    )


def _properties(cls: type) -> list[str]:
    return sorted(
        name
        for name in dir(cls)
        if not name.startswith("_") and isinstance(inspect.getattr_static(cls, name), property)
    )


@pytest.mark.parametrize("name", _coroutines(Analyzer))
def test_every_analyzer_method_has_a_blocking_twin(name: str) -> None:
    assert hasattr(SyncAnalyzer, name), f"SyncAnalyzer lacks {name}"
    assert_parity(getattr(Analyzer, name), getattr(SyncAnalyzer, name))


@pytest.mark.parametrize("name", _properties(Analyzer))
def test_every_analyzer_property_is_mirrored(name: str) -> None:
    assert isinstance(inspect.getattr_static(SyncAnalyzer, name, None), property)


def test_the_twins_are_blocking() -> None:
    for name in _coroutines(Analyzer):
        assert not inspect.iscoroutinefunction(getattr(SyncAnalyzer, name)), name


def test_open_device_and_fuji_open() -> None:
    assert_parity(open_device, Fuji.open, extra=frozenset({"portal"}))


def test_find_devices() -> None:
    assert_parity(find_devices, sync_find_devices, extra=frozenset({"portal"}))


def test_the_method_list_is_complete() -> None:
    # A guard on the guard: the coroutine scan finds the whole public surface.
    assert {"poll", "identify", "read_metadata", "snapshot", "close"} <= set(_coroutines(Analyzer))
