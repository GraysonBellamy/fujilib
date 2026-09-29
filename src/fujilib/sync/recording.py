"""Blocking recording (design §7.3, §7.6).

:func:`record` runs the async recorder on a portal's loop and yields a
:class:`SyncRecording`, iterated with a plain ``for`` loop::

    from fujilib.sync import Fuji, PollSourceAdapter, SyncCsvSink, pipe, record

    with (
        Fuji.open("COM8", channel_map={"CH3": "o2"}) as anz,
        record(PollSourceAdapter("zpa", anz), rate_hz=1.0, duration=60) as rec,
        SyncCsvSink("run.csv", portal=anz.portal) as sink,
    ):
        pipe(rec, sink)

Everything behaves as in :mod:`fujilib.streaming.recorder`; leaving the
``with`` block stops the recording, and raises the error that ended it, if
one did.
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Self

import anyio

from fujilib.errors import FujiValidationError
from fujilib.sinks.base import pipe as async_pipe
from fujilib.streaming.poll_source import PollSourceAdapter as AsyncPollSourceAdapter
from fujilib.streaming.recorder import OverflowPolicy
from fujilib.streaming.recorder import record as async_record
from fujilib.sync.sinks import SyncSinkAdapter

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence
    from concurrent.futures import Future

    from fujilib.devices.models import Frame
    from fujilib.sinks.base import SampleSink
    from fujilib.streaming.poll_source import DeviceResult, PollSource, SourceLayout
    from fujilib.streaming.recorder import (
        AcquisitionSummary,
        Batch,
        ReconnectPolicy,
        Recording,
    )
    from fujilib.sync.analyzer import SyncAnalyzer
    from fujilib.sync.portal import SyncPortal

__all__ = ["PollSourceAdapter", "SyncRecording", "pipe", "record"]


class PollSourceAdapter:
    """A :class:`~fujilib.sync.analyzer.SyncAnalyzer` as a poll source (unified API §E).

    The blocking twin of :class:`fujilib.streaming.poll_source.PollSourceAdapter`;
    it brings the analyzer's portal to :func:`record`.
    """

    __slots__ = ("_device", "_source")

    def __init__(self, name: str, device: SyncAnalyzer) -> None:
        """Publish ``device``'s polls under ``name``."""
        self._device = device
        self._source = AsyncPollSourceAdapter(name, device.analyzer)

    @property
    def name(self) -> str:
        """The name the analyzer's data is published under."""
        return self._source.name

    @property
    def device(self) -> SyncAnalyzer:
        """The wrapped analyzer."""
        return self._device

    @property
    def portal(self) -> SyncPortal:
        """The portal the analyzer's loop runs in."""
        return self._device.portal

    @property
    def source(self) -> AsyncPollSourceAdapter:
        """The async poll source, whose coroutines run on :attr:`portal`."""
        return self._source

    def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[Frame]]:
        """Blocking :meth:`fujilib.streaming.poll_source.PollSourceAdapter.poll`."""
        return self.portal.call(self._source.poll, names)

    def layout(self, names: Sequence[str] | None = None) -> Mapping[str, SourceLayout]:
        """:meth:`fujilib.streaming.poll_source.PollSourceAdapter.layout`."""
        return self._source.layout(names)

    def reconnect(self, name: str) -> None:
        """Blocking :meth:`fujilib.streaming.poll_source.PollSourceAdapter.reconnect`."""
        self.portal.call(self._source.reconnect, name)


class SyncRecording[T]:
    """A running recording, iterated with a plain ``for`` loop (unified API §I)."""

    def __init__(self, recording: Recording[T], portal: SyncPortal) -> None:
        """Wrap ``recording``, which runs on ``portal``'s loop; :func:`record` does this."""
        self._recording = recording
        self._portal = portal

    @property
    def stream(self) -> Self:
        """The batches, one per tick: this recording itself."""
        return self

    @property
    def summary(self) -> AcquisitionSummary:
        """The live counters."""
        return self._recording.summary

    @property
    def rate_hz(self) -> float:
        """The requested rate."""
        return self._recording.rate_hz

    @property
    def recording(self) -> Recording[T]:
        """The async recording, on :attr:`portal`."""
        return self._recording

    @property
    def portal(self) -> SyncPortal:
        """The portal the recording runs in."""
        return self._portal

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> T:
        try:
            return self._portal.call(self._recording.stream.receive)
        except anyio.EndOfStream:
            raise StopIteration from None


@contextmanager
def record(
    source: PollSourceAdapter | PollSource,
    *,
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    reconnect: ReconnectPolicy | None = None,
    portal: SyncPortal | None = None,
) -> Generator[SyncRecording[Batch]]:
    """Blocking :func:`fujilib.streaming.recorder.record`.

    ``source`` is a blocking :class:`PollSourceAdapter`, whose portal is used
    unless ``portal`` is given, or an async poll source with the ``portal``
    its analyzers run on.

    Raises:
        FujiValidationError: an async source without a ``portal``, or an
            argument :func:`~fujilib.streaming.recorder.record` refuses.
        FujiConnectionError: on leaving the block, when a connection failure
            ended the recording.
    """
    if isinstance(source, PollSourceAdapter):
        async_source: PollSource = source.source
        active = portal if portal is not None else source.portal
    else:
        if portal is None:
            msg = "an async poll source needs the portal its analyzers run on"
            raise FujiValidationError(msg)
        async_source, active = source, portal
    manager = async_record(
        async_source,
        rate_hz=rate_hz,
        duration=duration,
        names=names,
        overflow=overflow,
        buffer_size=buffer_size,
        reconnect=reconnect,
    )
    with active.wrap_async_context_manager(manager) as recording:
        yield SyncRecording(recording, active)


def pipe(
    source: SyncRecording[Batch],
    sink: SyncSinkAdapter | SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
) -> AcquisitionSummary:
    """Blocking :func:`fujilib.sinks.base.pipe`, on the recording's portal.

    ``sink`` is a blocking sink or an async one, open either way. Ctrl-C
    stops it: the pipe writes what it holds, then ``KeyboardInterrupt`` is
    raised here.
    """
    target = sink.async_sink if isinstance(sink, SyncSinkAdapter) else sink
    portal = source.portal
    scopes: list[anyio.CancelScope] = []

    async def run() -> AcquisitionSummary | None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            return await async_pipe(
                source.recording, target, batch_size=batch_size, flush_interval=flush_interval
            )
        return None  # cancelled by Ctrl-C

    future = portal.start_task_soon(run)
    try:
        summary = _wait(future)
    except KeyboardInterrupt:

        def stop() -> None:
            for scope in scopes:
                scope.cancel()

        portal.run_in_loop(stop)
        with contextlib.suppress(Exception):
            _ = _wait(future)  # the pipe's last write
        raise
    assert summary is not None  # noqa: S101 - only a cancelled run returns None
    return summary


#: How often a blocking wait wakes, so Ctrl-C is not held up (Windows delivers
#: it only between waits).
_WAKE_S: Final = 0.2


def _wait[T](future: Future[T]) -> T:
    """``future``'s result, waiting in short steps so Ctrl-C gets through."""
    while True:
        try:
            return future.result(timeout=_WAKE_S)
        except TimeoutError:
            continue
