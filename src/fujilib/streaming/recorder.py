"""The recorder: poll a source at a fixed rate into a bounded stream (design §7.6).

:func:`record` is an async context manager. It yields a :class:`Recording`
whose ``stream`` receives one *batch* per tick: a mapping from each
analyzer's name to its :class:`~fujilib.streaming.sample.Sample`::

    async with record(PollSourceAdapter("zpa", anz), rate_hz=1.0, duration=60) as rec:
        async for batch in rec.stream:
            sample = batch["zpa"]

**Rows keep their columns.** The source's layouts are read once, when the
recording starts, and every sample carries the channels established then, so
``sample_to_row(sample)`` gives every row the same keys, a failed poll
included. A channel established later is left out of this recording's rows
(design §13.1 #34).

**Schedule.** Ticks follow an absolute schedule: tick *k* is due ``k /
rate_hz`` seconds after the first, which runs at once. A poll that overruns
by one or more whole slots skips them and counts them in
``samples_late``; the recorder never catches up in a burst. ``max_drift_ms``
is the latest a poll started after its slot. A recording with a
``duration`` makes every tick due before the duration has passed,
``ceil(duration * rate_hz)`` of them (at least one), and its stream ends
after the last.

**Failures.** A failed poll is a sample with ``frame=None`` and ``error``
set, timed around the poll, so gaps are recorded; the recording goes on. A
connection failure (:class:`~fujilib.errors.FujiConnectionError`) ends it:
the tick's batch is still delivered, the stream ends, and leaving the
``async with`` block raises the error. With a :class:`ReconnectPolicy` the
recording goes on instead: every tick of the outage is an error sample, and
the analyzer is reopened on the policy's schedule.

**Overflow.** The stream holds ``buffer_size`` batches. When it is full,
:class:`OverflowPolicy` decides: wait (``BLOCK``, the consumer sets the pace
and ticks missed meanwhile are late), or drop a whole batch, the new one
(``DROP_NEWEST``) or the oldest (``DROP_OLDEST``), counted in
``samples_dropped``. A batch is never split. Batches still waiting in the
stream when the recording stops were never read, and are counted as dropped
too, so ``samples_emitted`` is then exactly what the consumer received.

For a recording that runs to its end,
``samples_emitted + samples_dropped + samples_late == target_total_samples``.
"""

from __future__ import annotations

import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

import anyio

from fujilib._groups import unwrap
from fujilib._logging import get_logger
from fujilib.devices.models import TransferTiming
from fujilib.errors import (
    FujiConnectionError,
    FujiError,
    FujiFrameError,
    FujiTransportError,
    FujiValidationError,
)
from fujilib.streaming.poll_source import PollSource
from fujilib.streaming.sample import Sample

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

    from fujilib.streaming.poll_source import SourceLayout

__all__ = [
    "AcquisitionSummary",
    "Batch",
    "OverflowPolicy",
    "ReconnectPolicy",
    "Recording",
    "record",
]

_LOG = get_logger("recorder")
_MS: Final = 1000.0

type Batch = Mapping[str, Sample]
"""One tick of a recording: each analyzer's sample, by name."""


class OverflowPolicy(StrEnum):
    """What the recorder does when the stream's buffer is full."""

    BLOCK = "block"
    """Wait for room. The consumer sets the pace; ticks missed meanwhile are late."""
    DROP_NEWEST = "drop_newest"
    """Discard the new batch."""
    DROP_OLDEST = "drop_oldest"
    """Discard the oldest waiting batch to make room for the new one."""


@dataclass(slots=True)
class AcquisitionSummary:
    """The counters of one recording (unified API §I, §M).

    Mutable: the recorder updates it while it runs, and sets ``finished_at``
    when it stops, however it stops. Every count is of polls (batches), not
    of rows (design §7.6).
    """

    started_at: datetime
    finished_at: datetime | None = None
    samples_emitted: int = 0
    """Batches put on the stream for the consumer and not dropped since; once the
    recording has stopped, exactly the batches the consumer received."""
    samples_late: int = 0
    """Ticks skipped because a poll overran their slots."""
    max_drift_ms: float = 0.0
    """The latest a poll started after its slot, in milliseconds."""
    target_total_samples: int | None = None
    """Ticks a recording with a ``duration`` is due to make; ``None`` without one."""
    samples_dropped: int = 0
    """Batches the overflow policy discarded, and batches still unread when the
    recording stopped."""
    error_samples: int = 0
    """Samples of failed polls (``frame=None``), counted as they are polled."""
    disconnects: int = 0
    """Connection failures: each outage a ``ReconnectPolicy`` rides out, and the one
    that ends a recording without one."""
    reconnects: int = 0
    """Outages that ended with the analyzer reopened."""


#: The default wait before each reconnection attempt, in seconds; the last repeats.
DEFAULT_BACKOFF_S: Final = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Reopen an analyzer after a connection failure instead of ending the recording.

    The recording keeps its schedule through an outage: every tick is an
    error sample until the analyzer is back. Attempts are made at ticks, the
    first ``backoff_s[0]`` seconds after the failure, the next ``backoff_s[1]``
    after that, and so on; the last wait repeats. An attempt takes as long as
    opening the port and identifying the analyzer, and delays the tick it is
    made in.

    An attempt that finds another analyzer, or any failure other than a
    connection, timeout or framing error, ends the recording.

    Attributes:
        backoff_s: Seconds to wait before each attempt.
        max_attempts: Attempts per outage before the recording ends with the
            last error; ``None`` for no limit.
    """

    backoff_s: tuple[float, ...] = DEFAULT_BACKOFF_S
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        """Check the schedule.

        Raises:
            FujiValidationError: an empty or negative schedule, or fewer than one attempt.
        """
        if not self.backoff_s or not all(_is_seconds(s) and s >= 0 for s in self.backoff_s):
            msg = f"backoff_s must be one or more finite, non-negative seconds: {self.backoff_s!r}"
            raise FujiValidationError(msg)
        attempts = self.max_attempts
        if attempts is not None and (not _is_int(attempts) or attempts < 1):
            msg = f"max_attempts must be None or at least 1, got {attempts!r}"
            raise FujiValidationError(msg)

    def delay(self, attempt: int) -> float:
        """The wait before attempt ``attempt`` (1 for the first) of an outage."""
        return self.backoff_s[min(attempt, len(self.backoff_s)) - 1]


@dataclass(slots=True)
class Recording[T]:
    """A running recording (unified API §I).

    Attributes:
        stream: The batches, one per tick; iterate it, or the recording itself.
        summary: The live counters.
        rate_hz: The requested rate.
    """

    stream: MemoryObjectReceiveStream[T]
    summary: AcquisitionSummary
    rate_hz: float

    def __aiter__(self) -> AsyncIterator[T]:
        """Iterate the batches of :attr:`stream`."""
        return self.stream.__aiter__()


class _Clock(Protocol):
    """What the schedule runs on; tests substitute a manual clock."""

    def now(self) -> float: ...

    async def sleep_until(self, deadline: float) -> None: ...


class _AnyioClock:
    """The event loop's clock."""

    def now(self) -> float:
        return anyio.current_time()

    async def sleep_until(self, deadline: float) -> None:
        await anyio.sleep_until(deadline)


_ANYIO_CLOCK: Final = _AnyioClock()


@asynccontextmanager
async def record(
    source: PollSource,
    *,
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    reconnect: ReconnectPolicy | None = None,
) -> AsyncGenerator[Recording[Batch]]:
    """Poll ``source`` at ``rate_hz`` for the ``async with`` block (see the module docstring).

    Args:
        source: The analyzers, e.g. a
            :class:`~fujilib.streaming.poll_source.PollSourceAdapter`. Each must
            have established channels: identify it, or assert its gases with
            ``channel_map``.
        rate_hz: Ticks per second. One analyzer's poll takes about 0.12 s, so
            about 7-8 Hz is the practical ceiling for one station (design §2.4).
        duration: Seconds to record; ``None`` records until the block exits.
        names: The analyzers to record; all of the source's when ``None``.
        overflow: What to do when ``buffer_size`` batches are waiting.
        buffer_size: Batches the stream holds.
        reconnect: Reopen an analyzer after a connection failure instead of
            ending the recording; each analyzer must be reopenable.

    Raises:
        FujiValidationError: an argument is invalid, a name is unknown, or an
            analyzer has no established channels (or cannot be reopened, with
            ``reconnect``); nothing was polled.
        FujiConnectionError: raised on leaving the block when a connection
            failure ended the recording.
    """
    async with _record(
        source,
        rate_hz=rate_hz,
        duration=duration,
        names=names,
        overflow=overflow,
        buffer_size=buffer_size,
        reconnect=reconnect,
        clock=_ANYIO_CLOCK,
    ) as recording:
        yield recording


@asynccontextmanager
async def _record(
    source: PollSource,
    *,
    rate_hz: float,
    duration: float | None,
    names: Sequence[str] | None,
    overflow: OverflowPolicy,
    buffer_size: int,
    reconnect: ReconnectPolicy | None,
    clock: _Clock,
) -> AsyncGenerator[Recording[Batch]]:
    """:func:`record` on ``clock``."""
    policy = _check_overflow(overflow)
    period, total = _check_timing(rate_hz, duration)
    if not _is_int(buffer_size) or buffer_size < 1:
        msg = f"buffer_size must be an integer of at least 1, got {buffer_size!r}"
        raise FujiValidationError(msg)
    if reconnect is not None and not _is_policy(reconnect):
        msg = f"reconnect must be a ReconnectPolicy or None, got {type(reconnect).__name__}"
        raise FujiValidationError(msg)
    layouts = _layouts(source, _check_names(names), reconnect)

    send, receive = anyio.create_memory_object_stream[Batch](max_buffer_size=buffer_size)
    summary = AcquisitionSummary(started_at=datetime.now(UTC), target_total_samples=total)
    producer = _Producer(
        source=source,
        layouts=layouts,
        send=send,
        evict=receive.clone(),
        period=period,
        total=total,
        overflow=policy,
        reconnect=reconnect,
        summary=summary,
        clock=clock,
    )
    _LOG.info(
        "recording %s at %g Hz for %s (overflow %s, buffer %d, reconnect %s)",
        ", ".join(layouts),
        rate_hz,
        f"{duration:g} s" if duration is not None else "until stopped",
        policy.value,
        buffer_size,
        "on" if reconnect is not None else "off",
    )
    failure: BaseException | None = None
    try:
        async with anyio.create_task_group() as tg, receive:
            _ = tg.start_soon(producer.run)
            try:
                yield Recording(stream=receive, summary=summary, rate_hz=rate_hz)
            finally:
                tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        failure = unwrap(group)
        if failure is group:
            raise
    finally:
        # Batches still waiting when the recording stops were never read.
        unread = receive.statistics().current_buffer_used
        summary.samples_emitted -= unread
        summary.samples_dropped += unread
        summary.finished_at = datetime.now(UTC)
        _LOG.info(
            "recording stopped: %d emitted, %d late, %d dropped, %d errors, "
            "%d disconnects, %d reconnects, max drift %.1f ms",
            summary.samples_emitted,
            summary.samples_late,
            summary.samples_dropped,
            summary.error_samples,
            summary.disconnects,
            summary.reconnects,
            summary.max_drift_ms,
        )
    if failure is not None:
        # Raised outside the handler, so it keeps its own cause and context.
        raise failure
    if producer.error is not None:
        raise producer.error


@dataclass(slots=True)
class _Outage:
    """One analyzer's connection outage."""

    next_attempt: float
    attempts: int = 0


@dataclass(slots=True, kw_only=True)
class _Producer:
    """The task that polls on schedule and publishes batches."""

    source: PollSource
    layouts: Mapping[str, SourceLayout]
    send: MemoryObjectSendStream[Batch]
    evict: MemoryObjectReceiveStream[Batch]
    period: float
    total: int | None
    overflow: OverflowPolicy
    reconnect: ReconnectPolicy | None
    summary: AcquisitionSummary
    clock: _Clock
    error: FujiError | None = None
    """The failure that ended the recording, raised when the block exits."""
    outages: dict[str, _Outage] = field(default_factory=dict[str, _Outage])
    failing: set[str] = field(default_factory=set[str])

    async def run(self) -> None:
        try:
            await self._loop()
        finally:
            # Ends the consumer's iteration once it has taken what is waiting.
            self.send.close()
            self.evict.close()

    async def _loop(self) -> None:
        clock, period, total, summary = self.clock, self.period, self.total, self.summary
        start = clock.now()
        tick = 0
        while total is None or tick < total:
            target = start + tick * period
            now = clock.now()
            if now >= target + period:
                missed = max(1, int((now - target) / period))
                if total is not None:
                    missed = min(missed, total - tick)
                summary.samples_late += missed
                tick += missed
                if total is not None and tick >= total:
                    break
                target = start + tick * period
            await clock.sleep_until(target)
            summary.max_drift_ms = max(summary.max_drift_ms, (clock.now() - target) * _MS)
            batch = await self._poll()
            await self._publish(batch)
            if not await self._after(batch):
                return
            tick += 1

    async def _poll(self) -> Batch:
        requested_at, t_request = datetime.now(UTC), time.monotonic_ns()
        results = await self.source.poll(tuple(self.layouts))
        t_reply, received_at = time.monotonic_ns(), datetime.now(UTC)
        timing = TransferTiming(
            requested_at=requested_at,
            received_at=received_at,
            t_request_mono_ns=t_request,
            t_reply_mono_ns=t_reply,
        )
        batch: dict[str, Sample] = {}
        for name, layout in self.layouts.items():
            result = results.get(name)
            frame = result.value if result is not None and result.error is None else None
            if frame is not None:
                batch[name] = Sample.from_frame(
                    frame, device=name, address=layout.address, channels=layout.channels
                )
                if name in self.failing:
                    self.failing.discard(name)
                    _LOG.info("%s: polling again", name)
                continue
            error = result.error if result is not None else None
            if error is None:
                error = FujiError(f"the source returned no result for {name!r}")
            batch[name] = Sample.from_error(
                error,
                device=name,
                address=layout.address,
                protocol=layout.protocol,
                timing=timing,
                channels=layout.channels,
            )
            self.summary.error_samples += 1
            if name not in self.failing:
                self.failing.add(name)
                _LOG.warning("%s: poll failed: %s", name, error)
            else:
                _LOG.debug("%s: poll failed: %s", name, error)
        return MappingProxyType(batch)

    async def _publish(self, batch: Batch) -> None:
        summary = self.summary
        if self.overflow is OverflowPolicy.BLOCK:
            await self.send.send(batch)
            summary.samples_emitted += 1
            return
        try:
            self.send.send_nowait(batch)
        except anyio.WouldBlock:
            pass
        else:
            summary.samples_emitted += 1
            return
        if self.overflow is OverflowPolicy.DROP_NEWEST:
            summary.samples_dropped += 1
            _LOG.debug("the stream is full: dropped the newest batch")
            return
        # DROP_OLDEST. The buffer is full, and nothing awaits between these
        # calls, so the oldest batch is there to evict and its place is free.
        _ = self.evict.receive_nowait()
        self.send.send_nowait(batch)
        summary.samples_dropped += 1
        _LOG.debug("the stream is full: dropped the oldest batch")

    async def _after(self, batch: Batch) -> bool:
        """Deal with connection failures; ``False`` ends the recording."""
        lost = [n for n, s in batch.items() if isinstance(s.error, FujiConnectionError)]
        for name in [n for n in self.outages if batch[n].error is None]:
            del self.outages[name]  # the source recovered without being reopened
            _LOG.info("%s: polling again without reconnecting", name)
        if not lost:
            return True
        policy = self.reconnect
        if policy is None:
            self.summary.disconnects += 1
            self.error = batch[lost[0]].error
            _LOG.error("%s: the connection failed; the recording ends", lost[0])
            return False
        now = self.clock.now()
        for name in lost:
            outage = self.outages.get(name)
            if outage is None:
                outage = self.outages[name] = _Outage(next_attempt=now + policy.delay(1))
                self.summary.disconnects += 1
                _LOG.warning("%s: the connection failed; reconnecting", name)
            if self.clock.now() >= outage.next_attempt and not await self._reopen(
                name, outage, policy
            ):
                return False
        return True

    async def _reopen(self, name: str, outage: _Outage, policy: ReconnectPolicy) -> bool:
        """One attempt to reopen ``name``; ``False`` ends the recording."""
        outage.attempts += 1
        try:
            await self.source.reconnect(name)
        except (FujiTransportError, FujiFrameError) as exc:
            if policy.max_attempts is not None and outage.attempts >= policy.max_attempts:
                self.error = exc
                _LOG.error("%s: gave up after %d attempts: %s", name, outage.attempts, exc)
                return False
            outage.next_attempt = self.clock.now() + policy.delay(outage.attempts + 1)
            _LOG.info("%s: reconnection attempt %d failed: %s", name, outage.attempts, exc)
            return True
        except FujiError as exc:
            self.error = exc
            _LOG.error("%s: cannot reconnect: %s", name, exc)
            return False
        del self.outages[name]
        self.summary.reconnects += 1
        _LOG.info("%s: reconnected after %d attempts", name, outage.attempts)
        return True


# --- Arguments ---------------------------------------------------------------------------------


def _is_policy(value: object) -> bool:
    return isinstance(value, ReconnectPolicy)


def _is_poll_source(value: object) -> bool:
    return isinstance(value, PollSource)


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_seconds(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _check_overflow(overflow: OverflowPolicy | str) -> OverflowPolicy:
    try:
        return OverflowPolicy(overflow)
    except ValueError:
        known = ", ".join(p.value for p in OverflowPolicy)
        msg = f"overflow must be one of {known}, got {overflow!r}"
        raise FujiValidationError(msg) from None


def _check_timing(rate_hz: float, duration: float | None) -> tuple[float, int | None]:
    """The period and the tick count."""
    if not _is_seconds(rate_hz) or rate_hz <= 0:
        msg = f"rate_hz must be a finite number above 0, got {rate_hz!r}"
        raise FujiValidationError(msg)
    if duration is None:
        return 1.0 / rate_hz, None
    if not _is_seconds(duration) or duration <= 0:
        msg = f"duration must be None or a finite number of seconds above 0, got {duration!r}"
        raise FujiValidationError(msg)
    # Every tick due before the duration has passed; rounding first keeps
    # 0.3 s at 10 Hz (3.0000000000000004 ticks) at 3.
    return 1.0 / rate_hz, max(1, math.ceil(round(duration * rate_hz, 9)))


def _check_names(names: Sequence[str] | None) -> tuple[str, ...] | None:
    if names is None:
        return None
    if isinstance(names, str) or not all(_is_text(n) for n in names):
        msg = "names must be a sequence of analyzer names, not one string"
        raise FujiValidationError(msg)
    return tuple(names)


def _layouts(
    source: PollSource, names: tuple[str, ...] | None, reconnect: ReconnectPolicy | None
) -> Mapping[str, SourceLayout]:
    if not _is_poll_source(source):
        msg = f"source must be a poll source, e.g. a PollSourceAdapter; got {type(source).__name__}"
        raise FujiValidationError(msg)
    layouts = dict(source.layout(names))
    unknown = [n for n in names or () if n not in layouts]
    if unknown:
        msg = f"the source has no analyzer named {', '.join(map(repr, unknown))}"
        raise FujiValidationError(msg)
    if not layouts:
        msg = "the source has no analyzers to record"
        raise FujiValidationError(msg)
    for name, layout in layouts.items():
        if not layout.channels:
            msg = (
                f"{name!r} has no established channels: identify it, or assert its gases "
                "with channel_map, before recording"
            )
            raise FujiValidationError(msg)
        if reconnect is not None and not layout.reopenable:
            msg = (
                f"{name!r} cannot be reopened (its port came from the caller); "
                "record without a reconnect policy"
            )
            raise FujiValidationError(msg)
    return MappingProxyType(layouts)
