"""Rows, schemas, the sink contract and :func:`pipe` (design §7.6).

**Rows.** :func:`sample_to_row` flattens a
:class:`~fujilib.streaming.sample.Sample` into one wide row, with columns
fixed by a set of channels:

- the header: ``device``, ``address``, ``protocol``, ``t_mono_ns``, ``t_utc``,
  ``t_midpoint_mono_ns``, ``requested_at``, ``received_at``, ``latency_s``;
- per channel ``N``, the columns of
  :data:`~fujilib.devices.models.READING_COLUMNS` prefixed ``chN_``
  (``ch3_value``, ``ch3_state``, …);
- the analyzer columns of :data:`~fujilib.devices.models.ANALYZER_COLUMNS`;
- ``error_type`` and ``error_message``, ``None`` on a successful poll.

Every value is ``float``, ``int``, ``str``, ``bool`` or ``None``; datetimes are
ISO 8601 strings. A successful row and an error row have exactly the same keys:
an error row carries ``None`` in every reading and analyzer column.

**Schemas.** A sink fixes its columns with a :class:`SchemaLock` before its
first row, from :func:`row_columns` for a set of channels, never from the
values, so a recording that starts with an error still types every reading
column. A sample with a channel the schema lacks is refused rather than
written without it.

**Sinks** follow :class:`SampleSink`: ``open``, ``write_many``, ``close``, and
``async with``. :func:`pipe` writes a recording's batches to one.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol, Self, runtime_checkable

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream

from fujilib._logging import get_logger
from fujilib.devices.models import ANALYZER_COLUMNS, READING_COLUMNS
from fujilib.errors import (
    FujiSinkError,
    FujiSinkSchemaError,
    FujiSinkWriteError,
    FujiValidationError,
)
from fujilib.sinks._schema import ColumnSpec
from fujilib.streaming.recorder import AcquisitionSummary, Recording

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from types import TracebackType

    from fujilib.devices.models import Scalar
    from fujilib.registry.channels import ChannelId
    from fujilib.streaming.recorder import Batch
    from fujilib.streaming.sample import Sample

__all__ = [
    "HEADER_COLUMNS",
    "BaseSink",
    "SampleSink",
    "SchemaLock",
    "pipe",
    "row_columns",
    "sample_channels",
    "sample_to_row",
]

_LOG = get_logger("sinks")

#: The header columns: ``(name, type, nullable)``.
HEADER_COLUMNS: Final[tuple[ColumnSpec, ...]] = (
    ColumnSpec("device", str, nullable=False),
    ColumnSpec("address", int, nullable=False),
    ColumnSpec("protocol", str, nullable=False),
    ColumnSpec("t_mono_ns", int, nullable=False),
    ColumnSpec("t_utc", str, nullable=False),
    ColumnSpec("t_midpoint_mono_ns", int, nullable=True),
    ColumnSpec("requested_at", str, nullable=False),
    ColumnSpec("received_at", str, nullable=False),
    ColumnSpec("latency_s", float, nullable=False),
)

_ERROR_COLUMNS: Final[tuple[ColumnSpec, ...]] = (
    ColumnSpec("error_type", str, nullable=True),
    ColumnSpec("error_message", str, nullable=True),
)

#: The longest :func:`pipe` waits to write what it holds when it is stopped.
_FINAL_WRITE_S: Final = 30.0


def _prefix(channel: ChannelId) -> str:
    return f"ch{channel.number}_"


def row_columns(channels: Iterable[ChannelId]) -> tuple[ColumnSpec, ...]:
    """The columns of every row for an analyzer with these established ``channels``.

    Reading and analyzer columns are nullable, because an error row carries
    ``None`` in all of them.
    """
    specs = list(HEADER_COLUMNS)
    for channel in channels:
        prefix = _prefix(channel)
        specs.extend(
            ColumnSpec(prefix + c.name, c.python_type, nullable=True) for c in READING_COLUMNS
        )
    specs.extend(ColumnSpec(c.name, c.python_type, nullable=True) for c in ANALYZER_COLUMNS)
    specs.extend(_ERROR_COLUMNS)
    return tuple(specs)


def sample_channels(sample: Sample) -> tuple[ChannelId, ...]:
    """The channels ``sample``'s row has columns for.

    :attr:`Sample.channels <fujilib.streaming.sample.Sample.channels>`, or,
    for a sample made without them, its frame's.
    """
    if sample.channels:
        return sample.channels
    return sample.frame.channels if sample.frame is not None else ()


def sample_to_row(
    sample: Sample,
    channels: Iterable[ChannelId] | None = None,
) -> dict[str, Scalar]:
    """Flatten ``sample`` into one wide row; see the module docstring for the layout.

    Args:
        sample: The sample to flatten.
        channels: The channels that fix the row's columns. When omitted, the
            sample's own (:func:`sample_channels`): a recorder sets them on
            every sample, failed polls included, so every row of a recording
            has the same keys.
    """
    frame = sample.frame
    established = tuple(channels) if channels is not None else sample_channels(sample)
    row: dict[str, Scalar] = {
        "device": sample.device,
        "address": sample.address,
        "protocol": sample.protocol.value,
        "t_mono_ns": sample.t_mono_ns,
        "t_utc": sample.t_utc.isoformat(),
        "t_midpoint_mono_ns": sample.t_midpoint_mono_ns,
        "requested_at": sample.requested_at.isoformat(),
        "received_at": sample.received_at.isoformat(),
        "latency_s": sample.latency_s,
    }
    readings = {r.channel: r for r in frame.readings} if frame is not None else {}
    for channel in established:
        prefix = _prefix(channel)
        reading = readings.get(channel)
        values = reading.as_dict() if reading is not None else {}
        row.update({prefix + c.name: values.get(c.name) for c in READING_COLUMNS})
    analyzer = frame.analyzer.as_dict() if frame is not None and frame.analyzer is not None else {}
    row.update({c.name: analyzer.get(c.name) for c in ANALYZER_COLUMNS})
    error = sample.error
    if error is not None:
        cls = type(error)
        row["error_type"] = f"{cls.__module__}.{cls.__qualname__}"
        row["error_message"] = str(error)
    else:
        row["error_type"] = None
        row["error_message"] = None
    return row


# --- Schemas ---------------------------------------------------------------------------------


class SchemaLock:
    """A sink's columns, fixed once from a set of channels.

    Locked explicitly with :meth:`lock`, or by the first batch
    :meth:`rows` sees, to the channels its samples carry (their union, in
    channel order). Types always come from :func:`row_columns`.
    """

    __slots__ = ("_channels", "_columns", "_sink")

    def __init__(self, sink: str, channels: Iterable[ChannelId] | None = None) -> None:
        """A lock for the sink named ``sink``, locked now if ``channels`` are given."""
        self._sink = sink
        self._channels: tuple[ChannelId, ...] | None = None
        self._columns: tuple[ColumnSpec, ...] = ()
        if channels is not None:
            _ = self.lock(channels)

    @property
    def is_locked(self) -> bool:
        """Whether the columns are fixed."""
        return self._channels is not None

    @property
    def channels(self) -> tuple[ChannelId, ...]:
        """The locked channels; empty before locking."""
        return self._channels or ()

    @property
    def columns(self) -> tuple[ColumnSpec, ...]:
        """The locked columns, in row order; empty before locking."""
        return self._columns

    def lock(self, channels: Iterable[ChannelId]) -> tuple[ColumnSpec, ...]:
        """Fix the columns to those of ``channels``; locking again to the same is allowed.

        Raises:
            FujiSinkSchemaError: the lock holds other channels already.
        """
        ordered = tuple(sorted(set(channels), key=lambda c: c.number))
        if self._channels is not None:
            if ordered != self._channels:
                msg = (
                    f"{self._sink}: the columns are locked to {_names(self._channels)}, "
                    f"not {_names(ordered)}"
                )
                raise FujiSinkSchemaError(msg)
            return self._columns
        self._channels = ordered
        self._columns = row_columns(ordered)
        return self._columns

    def rows(self, samples: Sequence[Sample]) -> list[dict[str, Scalar]]:
        """Each sample's row; unlocked columns are locked to this batch's channels first.

        Raises:
            FujiSinkSchemaError: a sample has a channel the columns lack.
        """
        if self._channels is None:
            _ = self.lock(c for s in samples for c in sample_channels(s))
        locked = self.channels
        for sample in samples:
            extra = set(sample_channels(sample)) - set(locked)
            if extra:
                msg = (
                    f"{self._sink}: the sample of {sample.device!r} has "
                    f"{_names(sorted(extra, key=lambda c: c.number))}, which the columns, "
                    f"locked to {_names(locked)}, do not"
                )
                raise FujiSinkSchemaError(msg)
        return [sample_to_row(s, locked) for s in samples]


def _is_memory_stream(value: object) -> bool:
    return isinstance(value, MemoryObjectReceiveStream)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_seconds(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _names(channels: Iterable[ChannelId]) -> str:
    return ", ".join(c.value for c in channels) or "no channels"


# --- Sinks ---------------------------------------------------------------------------------


@runtime_checkable
class SampleSink(Protocol):
    """Where :func:`pipe` writes samples.

    ``open`` and ``close`` are idempotent; ``async with`` opens and closes.
    """

    async def open(self) -> None:
        """Get ready to write."""
        ...

    async def write_many(self, samples: Sequence[Sample]) -> None:
        """Write ``samples``, in order."""
        ...

    async def close(self) -> None:
        """Finish writing and release what the sink holds."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class BaseSink:
    """The lifecycle the bundled sinks share: open once, write, close once.

    A subclass implements :meth:`_open`, :meth:`_write` (given rows under the
    locked schema) and :meth:`_close`. Closing runs even when the caller is
    cancelled.
    """

    def __init__(self, name: str, channels: Iterable[ChannelId] | None = None) -> None:
        """A sink called ``name``; its columns are locked now if ``channels`` are given."""
        self._name = name
        self._schema = SchemaLock(name, channels)
        self._state = "new"

    @property
    def schema(self) -> SchemaLock:
        """The sink's columns."""
        return self._schema

    @property
    def is_open(self) -> bool:
        """Whether the sink is open for writing."""
        return self._state == "open"

    async def open(self) -> None:
        """Open the sink; again is a no-op.

        Raises:
            FujiSinkError: the sink was closed; a sink is used once.
            FujiSinkWriteError: the backing store cannot be opened.
            FujiSinkDependencyError: an optional library the sink needs is missing.
        """
        if self._state == "open":
            return
        if self._state == "closed":
            msg = f"{self._name}: the sink was closed; open a new one"
            raise FujiSinkError(msg)
        await self._open()
        self._state = "open"

    async def write_many(self, samples: Sequence[Sample]) -> None:
        """Write ``samples``; an empty sequence writes nothing.

        Raises:
            FujiSinkError: the sink is not open.
            FujiSinkSchemaError: a sample has a channel the locked columns lack.
            FujiSinkWriteError: the backing store refused the write.
        """
        if self._state != "open":
            msg = f"{self._name}: write_many() needs an open sink"
            raise FujiSinkError(msg)
        if not samples:
            return
        await self._write(samples, self._schema.rows(samples))

    async def close(self) -> None:
        """Close the sink; again is a no-op. Completes even when the caller is cancelled.

        Raises:
            FujiSinkWriteError: the backing store failed to finish.
        """
        if self._state == "closed":
            return
        opened, self._state = self._state == "open", "closed"
        if opened:
            with anyio.CancelScope(shield=True):
                await self._close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _open(self) -> None:
        """Open the backing store."""

    async def _write(self, samples: Sequence[Sample], rows: list[dict[str, Scalar]]) -> None:
        """Write ``rows``, the rows of ``samples`` under the locked columns."""
        raise NotImplementedError

    async def _close(self) -> None:
        """Finish the backing store."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._name} {self._state}>"


# --- pipe ------------------------------------------------------------------------------------


async def pipe(
    source: Recording[Batch] | MemoryObjectReceiveStream[Batch],
    sink: SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
) -> AcquisitionSummary:
    """Write every batch of a recording to the open ``sink``, until the stream ends.

    Samples are written in groups: once ``batch_size`` are waiting, and at the
    latest ``flush_interval`` seconds after the first of them arrived, even if
    no more come. When the pipe stops, by the stream ending, an error or
    cancellation, it takes the batches already waiting in the stream and writes
    everything it holds first (for up to 30 s, even when cancelled), unless the
    sink itself failed. So a recording stopped with Ctrl-C has a row for every
    poll its summary counts.

    Returns:
        What the pipe wrote: ``samples_emitted`` counts batches (polls), and
        ``error_samples`` the samples of failed polls. The recording's own
        counters are in its ``summary``.

    Raises:
        FujiValidationError: ``source`` is not a recording or a memory object
            stream, or ``batch_size`` or ``flush_interval`` is invalid.
        FujiSinkError: the sink failed.
        FujiSinkWriteError: the last samples could not be written in time.
    """
    stream = source.stream if isinstance(source, Recording) else source
    if not _is_memory_stream(stream):
        msg = f"source must be a Recording or its stream, got {type(source).__name__}"
        raise FujiValidationError(msg)
    if not _is_count(batch_size) or batch_size < 1:
        msg = f"batch_size must be an integer of at least 1, got {batch_size!r}"
        raise FujiValidationError(msg)
    if not _is_seconds(flush_interval) or flush_interval <= 0:
        msg = f"flush_interval must be a finite number of seconds above 0, got {flush_interval!r}"
        raise FujiValidationError(msg)
    writer = _PipeWriter(sink)
    try:
        await writer.run(stream, batch_size, flush_interval)
    finally:
        writer.drain(stream)
        await writer.finish()
    if writer.unwritten:
        msg = f"could not write the last {writer.unwritten} samples within {_FINAL_WRITE_S:g} s"
        raise FujiSinkWriteError(msg)
    return writer.summary


class _PipeWriter:
    """What :func:`pipe` holds between writes."""

    def __init__(self, sink: SampleSink) -> None:
        self.sink = sink
        self.summary = AcquisitionSummary(started_at=datetime.now(UTC))
        self.pending: list[Sample] = []
        self.failed = False
        self.unwritten = 0
        """Samples the final write gave up on."""

    async def run(
        self, stream: MemoryObjectReceiveStream[Batch], batch_size: int, flush_interval: float
    ) -> None:
        due = math.inf
        while True:
            batch: Batch | None = None
            with anyio.move_on_at(due):
                try:
                    batch = await stream.receive()
                except anyio.EndOfStream:
                    return
            if batch is not None:
                if not self.pending:
                    due = anyio.current_time() + flush_interval
                self.take(batch)
                if len(self.pending) < batch_size and anyio.current_time() < due:
                    continue
            await self.flush()
            due = math.inf

    def take(self, batch: Batch) -> None:
        samples = list(batch.values())
        self.pending.extend(samples)
        self.summary.samples_emitted += 1
        self.summary.error_samples += sum(s.error is not None for s in samples)

    async def flush(self) -> None:
        samples = self.pending[:]
        self.pending.clear()
        try:
            await self.sink.write_many(samples)
        except anyio.get_cancelled_exc_class():
            raise  # the bundled sinks finish a write they have started
        except BaseException:
            self.failed = True
            raise

    def drain(self, stream: MemoryObjectReceiveStream[Batch]) -> None:
        """Take the batches already waiting in ``stream``, unless the sink failed."""
        if self.failed:
            return
        while True:
            try:
                batch = stream.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                return
            self.take(batch)

    async def finish(self) -> None:
        """Write what is left, even when cancelled, unless the sink failed."""
        if self.pending and not self.failed:
            count = len(self.pending)
            with anyio.move_on_after(_FINAL_WRITE_S, shield=True) as scope:
                await self.flush()
            if scope.cancelled_caught:
                self.unwritten = count
                _LOG.error("could not write the last %d samples in time", count)
        self.summary.finished_at = datetime.now(UTC)
