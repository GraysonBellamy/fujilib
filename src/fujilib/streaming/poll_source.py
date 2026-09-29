"""Poll sources: ``DeviceResult``, ``PollSource`` and ``PollSourceAdapter`` (unified API §E).

A recorder polls a *poll source*: an object whose ``poll(names)`` returns
one :class:`DeviceResult` per analyzer, keyed by name, holding either a
:class:`~fujilib.devices.models.Frame` or the error of a failed poll.
:class:`PollSourceAdapter` makes one :class:`~fujilib.devices.analyzer.Analyzer`
such a source, under a name that follows its data into rows.

A source also describes each analyzer with a :class:`SourceLayout`: its
station, protocol and established channels. The recorder reads the layouts
once, when it starts, so every sample of a recording, successful or not, has
the same row columns (design §7.6, §13.1 #34).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

from fujilib.errors import FujiError, FujiValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.models import Frame
    from fujilib.protocol.base import ProtocolKind
    from fujilib.registry.channels import ChannelId

__all__ = ["DeviceResult", "PollSource", "PollSourceAdapter", "SourceLayout"]


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


@dataclass(frozen=True, slots=True)
class SourceLayout:
    """What a recorder needs to know about one analyzer of a source before it polls.

    Attributes:
        address: The station number, 1-31.
        protocol: The wire protocol.
        channels: The established channels, in order: the row columns.
        reopenable: Whether the source can open the analyzer again after a
            connection failure (:meth:`PollSource.reconnect`).
    """

    address: int
    protocol: ProtocolKind
    channels: tuple[ChannelId, ...]
    reopenable: bool = False


@runtime_checkable
class PollSource(Protocol):
    """What :func:`~fujilib.streaming.recorder.record` polls (design §7.6)."""

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        """Poll the named analyzers (all when ``None``) once; a failure is a failed result."""
        ...

    def layout(self, names: Sequence[str] | None = None) -> Mapping[str, SourceLayout]:
        """Describe the named analyzers (all when ``None``), with no I/O."""
        ...

    async def reconnect(self, name: str) -> None:
        """Open analyzer ``name`` again after a connection failure.

        Raises:
            FujiError: it could not be opened, or is not the analyzer it was.
        """
        ...


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

    def _selected(self, names: Sequence[str] | None) -> bool:
        return names is None or self._name in set(names)

    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        """Poll the analyzer once: two transactions.

        Returns ``{name: result}``, or an empty mapping when ``names`` leaves
        this analyzer out. A failed poll is a failed result, not an exception.
        """
        if not self._selected(names):
            return MappingProxyType({})
        result: DeviceResult[Frame]
        try:
            result = DeviceResult.success(await self._device.poll())
        except FujiError as exc:
            result = DeviceResult(value=None, error=exc)
        return MappingProxyType({self._name: result})

    def layout(self, names: Sequence[str] | None = None) -> Mapping[str, SourceLayout]:
        """The analyzer's station, protocol and established channels, under its name.

        An empty mapping when ``names`` leaves this analyzer out.
        """
        if not self._selected(names):
            return MappingProxyType({})
        device = self._device
        layout = SourceLayout(
            address=device.address,
            protocol=device.protocol,
            channels=tuple(c.channel for c in device.channels),
            reopenable=device.session.reopenable,
        )
        return MappingProxyType({self._name: layout})

    async def reconnect(self, name: str) -> None:
        """Reopen the analyzer (:meth:`Analyzer.reopen <fujilib.devices.analyzer.Analyzer.reopen>`).

        Raises:
            FujiValidationError: ``name`` is not this source's name.
            FujiError: the analyzer could not be reopened.
        """
        if name != self._name:
            msg = f"this source publishes {self._name!r}, not {name!r}"
            raise FujiValidationError(msg)
        _ = await self._device.reopen()

    def __repr__(self) -> str:
        return f"<PollSourceAdapter {self._name!r} {self._device!r}>"
