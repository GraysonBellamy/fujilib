"""Timed samples, poll sources and the recorder (design §7.6).

- :class:`Sample` — one poll of one analyzer, with its timing.
- :class:`PollSourceAdapter` — an analyzer as a :class:`PollSource`.
- :func:`record` — poll a source at a fixed rate into a :class:`Recording`.
"""

from __future__ import annotations

from fujilib.streaming.poll_source import (
    DeviceResult,
    PollSource,
    PollSourceAdapter,
    SourceLayout,
)
from fujilib.streaming.recorder import (
    AcquisitionSummary,
    Batch,
    OverflowPolicy,
    ReconnectPolicy,
    Recording,
    record,
)
from fujilib.streaming.sample import Sample

__all__ = [
    "AcquisitionSummary",
    "Batch",
    "DeviceResult",
    "OverflowPolicy",
    "PollSource",
    "PollSourceAdapter",
    "ReconnectPolicy",
    "Recording",
    "Sample",
    "SourceLayout",
    "record",
]
