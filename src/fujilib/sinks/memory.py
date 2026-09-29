"""A sink that keeps samples in memory: for tests, notebooks and short recordings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fujilib.sinks.base import BaseSink

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from fujilib.devices.models import Scalar
    from fujilib.registry.channels import ChannelId
    from fujilib.streaming.sample import Sample

__all__ = ["InMemorySink"]


class InMemorySink(BaseSink):
    """Keeps every sample written, in order. It grows without bound.

    Example::

        async with InMemorySink() as sink, record(source, rate_hz=1, duration=10) as rec:
            await pipe(rec, sink)
        rows = sink.rows()
    """

    def __init__(self, *, channels: Iterable[ChannelId] | None = None) -> None:
        """An empty sink; its columns are locked now if ``channels`` are given."""
        super().__init__("memory", channels)
        self._samples: list[Sample] = []
        self._rows: list[dict[str, Scalar]] = []

    @property
    def samples(self) -> list[Sample]:
        """The samples written, in order. Kept after :meth:`close`."""
        return self._samples

    def rows(self) -> list[dict[str, Scalar]]:
        """The rows written, under the sink's locked columns."""
        return list(self._rows)

    async def _write(self, samples: Sequence[Sample], rows: list[dict[str, Scalar]]) -> None:
        self._samples.extend(samples)
        self._rows.extend(rows)
