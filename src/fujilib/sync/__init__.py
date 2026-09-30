"""The blocking facade over the async core (design §7.3).

- :class:`Fuji` — ``with Fuji.open("COM8") as anz:`` opens a :class:`SyncAnalyzer`;
  its ``manual_calibration()`` is a :class:`SyncRemoteCalibration`.
- :func:`find_devices` — blocking discovery.
- :func:`record`, :func:`pipe`, :class:`PollSourceAdapter` and the ``Sync*Sink``
  classes — blocking recording (design §7.6).
- :class:`SyncPortal` — the background event loop, shareable between them.
"""

from __future__ import annotations

from fujilib.sync.analyzer import Fuji, SyncAnalyzer, SyncRemoteCalibration
from fujilib.sync.discovery import find_devices
from fujilib.sync.portal import SyncPortal
from fujilib.sync.recording import PollSourceAdapter, SyncRecording, pipe, record
from fujilib.sync.sinks import SyncCsvSink, SyncInMemorySink, SyncParquetSink, SyncSinkAdapter

__all__ = [
    "Fuji",
    "PollSourceAdapter",
    "SyncAnalyzer",
    "SyncCsvSink",
    "SyncInMemorySink",
    "SyncParquetSink",
    "SyncPortal",
    "SyncRecording",
    "SyncRemoteCalibration",
    "SyncSinkAdapter",
    "find_devices",
    "pipe",
    "record",
]
