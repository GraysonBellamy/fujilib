"""Blocking sinks (design §7.3, §7.6).

Each is the async sink of the same name behind a :class:`SyncSinkAdapter`,
which opens, writes and closes it on a portal's loop: the ``portal`` given,
or one of its own for the ``with`` block. Pass the analyzer's portal
(``anz.portal``) to share one loop.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Self

from fujilib.errors import FujiSinkError
from fujilib.sinks.csv import CsvSink
from fujilib.sinks.memory import InMemorySink
from fujilib.sinks.parquet import DEFAULT_ROW_GROUP_SIZE, ParquetSink
from fujilib.sync.portal import SyncPortal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from os import PathLike
    from types import TracebackType

    from fujilib.devices.models import Scalar
    from fujilib.registry.channels import ChannelId
    from fujilib.sinks.base import SampleSink
    from fujilib.sinks.parquet import Compression
    from fujilib.streaming.sample import Sample

__all__ = ["SyncCsvSink", "SyncInMemorySink", "SyncParquetSink", "SyncSinkAdapter"]


class SyncSinkAdapter:
    """A blocking view of an async :class:`~fujilib.sinks.base.SampleSink`."""

    def __init__(self, sink: SampleSink, *, portal: SyncPortal | None = None) -> None:
        """Wrap ``sink``; without ``portal`` it gets a portal of its own when opened."""
        self._sink = sink
        self._portal = portal
        self._owns_portal = portal is None
        self._stack = ExitStack()
        self._closed = False

    @property
    def async_sink(self) -> SampleSink:
        """The async sink."""
        return self._sink

    def _active(self) -> SyncPortal:
        if self._portal is None:
            self._portal = self._stack.enter_context(SyncPortal())
        return self._portal

    def open(self) -> None:
        """Blocking ``open``; if it fails, a portal of the sink's own stops again."""
        try:
            self._active().call(self._sink.open)
        except BaseException:
            self._stack.close()
            if self._owns_portal:
                self._portal = None
            raise

    def write_many(self, samples: Sequence[Sample]) -> None:
        """Blocking ``write_many``."""
        self._active().call(self._sink.write_many, samples)

    def close(self) -> None:
        """Blocking ``close``; a portal of the sink's own stops with it. Again is a no-op.

        Raises:
            FujiSinkError: the shared portal stopped before the sink was closed, so
                the sink could not finish (a Parquet file would be unreadable).
        """
        if self._closed:
            return
        self._closed = True
        try:
            portal = self._portal
            if portal is None:
                return  # never opened
            if not portal.running:
                msg = "the sink's portal stopped before the sink was closed; close it first"
                raise FujiSinkError(msg)
            portal.call(self._sink.close)
        finally:
            self._stack.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class SyncInMemorySink(SyncSinkAdapter):
    """Blocking :class:`~fujilib.sinks.memory.InMemorySink`."""

    def __init__(
        self, *, channels: Iterable[ChannelId] | None = None, portal: SyncPortal | None = None
    ) -> None:
        """An empty sink; its columns are locked now if ``channels`` are given."""
        self._memory = InMemorySink(channels=channels)
        super().__init__(self._memory, portal=portal)

    @property
    def samples(self) -> list[Sample]:
        """The samples written, in order."""
        return self._memory.samples

    def rows(self) -> list[dict[str, Scalar]]:
        """The rows written, under the sink's locked columns."""
        return self._memory.rows()


class SyncCsvSink(SyncSinkAdapter):
    """Blocking :class:`~fujilib.sinks.csv.CsvSink`."""

    def __init__(
        self,
        path: str | PathLike[str],
        *,
        channels: Iterable[ChannelId] | None = None,
        portal: SyncPortal | None = None,
    ) -> None:
        """A sink for ``path``; its columns are locked now if ``channels`` are given."""
        super().__init__(CsvSink(path, channels=channels), portal=portal)


class SyncParquetSink(SyncSinkAdapter):
    """Blocking :class:`~fujilib.sinks.parquet.ParquetSink`."""

    def __init__(
        self,
        path: str | PathLike[str],
        *,
        channels: Iterable[ChannelId] | None = None,
        compression: Compression = "zstd",
        row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
        metadata: Mapping[str, str] | None = None,
        portal: SyncPortal | None = None,
    ) -> None:
        """A sink for ``path``; the arguments are :class:`~fujilib.sinks.parquet.ParquetSink`'s."""
        sink = ParquetSink(
            path,
            channels=channels,
            compression=compression,
            row_group_size=row_group_size,
            metadata=metadata,
        )
        super().__init__(sink, portal=portal)
