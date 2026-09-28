"""``DeviceResult`` and ``PollSourceAdapter`` (unified API §E, design §7.6).

A recorder polls a *poll source*: an object whose ``poll(names)`` returns
one :class:`DeviceResult` per analyzer, keyed by name, holding either a
:class:`~fujilib.devices.models.Frame` or the error of a failed poll.
:class:`PollSourceAdapter` makes one :class:`~fujilib.devices.analyzer.Analyzer`
such a source, under a name that follows its data into rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Self

from fujilib.errors import FujiError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.models import Frame

__all__ = ["DeviceResult", "PollSourceAdapter"]


@dataclass(frozen=True, slots=True)
class DeviceResult[T]:
    """One device's outcome: a value or an error, never both.

    Attributes:
        value: The result, or ``None`` when the call failed.
        error: The error, or ``None`` when the call succeeded.
    """

    value: T | None
    error: FujiError | None

    @property
    def ok(self) -> bool:
        """Whether the device produced a value."""
        return self.error is None

    @classmethod
    def success[V](cls, value: V) -> DeviceResult[V]:
        """A successful result holding ``value``."""
        del cls
        return DeviceResult(value=value, error=None)

    @classmethod
    def failure(cls, error: FujiError) -> Self:
        """A failed result holding ``error``."""
        return cls(value=None, error=error)


class PollSourceAdapter:
    """One :class:`~fujilib.devices.analyzer.Analyzer` as a poll source.

    Example::

        source = PollSourceAdapter("zpa", analyzer)
        results = await source.poll()  # {"zpa": DeviceResult(frame, None)}

    The parameter is ``device``, as in every sibling library (unified API §E).
    """

    __slots__ = ("_device", "_name")

    def __init__(self, name: str, device: Analyzer) -> None:
        """Publish ``device``'s polls under ``name``."""
        self._name = name
        self._device = device

    @property
    def name(self) -> str:
        """The name the analyzer's data is published under."""
        return self._name

    @property
    def device(self) -> Analyzer:
        """The wrapped analyzer."""
        return self._device

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        """Poll the analyzer once: two transactions.

        Returns ``{name: result}``, or an empty mapping when ``names`` leaves
        this analyzer out. A failed poll is a failed result, not an exception.
        """
        if names is not None and self._name not in set(names):
            return MappingProxyType({})
        result: DeviceResult[Frame]
        try:
            result = DeviceResult.success(await self._device.poll())
        except FujiError as exc:
            result = DeviceResult(value=None, error=exc)
        return MappingProxyType({self._name: result})
