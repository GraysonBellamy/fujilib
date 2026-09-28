"""The blocking facade over the async core (design §7.3).

- :class:`Fuji` — ``with Fuji.open("COM8") as anz:`` opens a :class:`SyncAnalyzer`.
- :func:`find_devices` — blocking discovery.
- :class:`SyncPortal` — the background event loop, shareable between them.
"""

from __future__ import annotations

from fujilib.sync.analyzer import Fuji, SyncAnalyzer
from fujilib.sync.discovery import find_devices
from fujilib.sync.portal import SyncPortal

__all__ = ["Fuji", "SyncAnalyzer", "SyncPortal", "find_devices"]
