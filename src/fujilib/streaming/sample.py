"""Timed sample: one poll of one analyzer, with its timing provenance (unified API §C).

One :class:`Sample` per analyzer per tick carries the whole :class:`Frame`
(design §7.6, §13.1 #13), so analyzer-level status travels once with every
channel. The unified timestamp contract:

- :attr:`Sample.t_mono_ns` — the monotonic acquisition time, the join key. It
  is the request/reply midpoint of the block that holds every concentration.
- :attr:`Sample.t_utc` — the same instant on the wall clock (UTC, tz-aware).
- :attr:`Sample.t_midpoint_mono_ns` — an integration-window midpoint. Always
  ``None``: a configured averaging period does not reveal the actual
  integration window, so response-time and averaging settings are reported in
  the analyzer metadata instead of shifting timestamps.

``requested_at``, ``received_at`` and ``latency_s`` are those of the same
concentration block, so ``t_utc`` is their midpoint. The status block's timing
is in ``Frame.status_timing``.

A failed poll is still a sample: ``frame`` is ``None``, ``error`` is set, and
the timing of the attempt is kept, so gaps are recorded rather than dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from fujilib.devices.models import Frame, TransferTiming
    from fujilib.errors import FujiError
    from fujilib.protocol.base import ProtocolKind

__all__ = ["Sample"]


def _empty_metadata() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Sample:
    """One poll of one analyzer, with full timing provenance.

    Attributes:
        device: The name that follows the data into sinks.
        address: The analyzer's station number.
        frame: Every established channel and the analyzer status, or ``None``
            when the poll failed.
        protocol: The wire protocol, kept for error rows too.
        t_mono_ns: Monotonic acquisition time: the concentration block's
            request/reply midpoint, in nanoseconds.
        t_utc: The same instant on the wall clock.
        requested_at: Wall clock just before the concentration block was requested.
        received_at: Wall clock just after its reply (or failure) was seen.
        latency_s: ``received_at - requested_at`` in seconds.
        t_midpoint_mono_ns: Integration-window midpoint; always ``None``.
        metadata: Free-form annotations. Not written to rows.
        error: The error of a failed poll, or ``None``.
    """

    device: str
    address: int
    frame: Frame | None
    protocol: ProtocolKind
    t_mono_ns: int
    t_utc: datetime
    requested_at: datetime
    received_at: datetime
    latency_s: float
    t_midpoint_mono_ns: int | None = None
    metadata: Mapping[str, str] = field(default_factory=_empty_metadata)
    error: FujiError | None = None

    @classmethod
    def from_frame(
        cls,
        frame: Frame,
        *,
        device: str,
        address: int,
        metadata: Mapping[str, str] | None = None,
    ) -> Sample:
        """Build the sample of a successful poll, timed by its concentration block."""
        timing = frame.readings_timing
        return cls(
            device=device,
            address=address,
            frame=frame,
            protocol=frame.protocol,
            t_mono_ns=timing.midpoint_mono_ns,
            t_utc=timing.midpoint_utc,
            requested_at=timing.requested_at,
            received_at=timing.received_at,
            latency_s=timing.latency_s,
            metadata=MappingProxyType(dict(metadata or {})),
        )

    @classmethod
    def from_error(
        cls,
        error: FujiError,
        *,
        device: str,
        address: int,
        protocol: ProtocolKind,
        timing: TransferTiming,
        metadata: Mapping[str, str] | None = None,
    ) -> Sample:
        """Build the sample of a failed poll, timed by the failed attempt."""
        return cls(
            device=device,
            address=address,
            frame=None,
            protocol=protocol,
            t_mono_ns=timing.midpoint_mono_ns,
            t_utc=timing.midpoint_utc,
            requested_at=timing.requested_at,
            received_at=timing.received_at,
            latency_s=timing.latency_s,
            metadata=MappingProxyType(dict(metadata or {})),
            error=error,
        )
