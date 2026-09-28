"""Identity and health snapshots (unified API §H).

``snapshot()`` builds these from cached state and never performs I/O. The base
:class:`DeviceSnapshot` has the same fields in every sibling library, so a
consumer can render every device's snapshot uniformly; :class:`FujiDeviceSnapshot`
adds what is specific to the analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from fujilib.devices.capability import Availability, Capability
    from fujilib.errors import ErrorContext
    from fujilib.protocol.base import ProtocolKind
    from fujilib.registry.channels import ChannelId

__all__ = ["DeviceSnapshot", "FujiDeviceSnapshot"]


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Cross-library identity and health snapshot.

    Attributes:
        name: The device's name (a manager name, or the model when unmanaged).
        model: Cached model, e.g. ``"ZPA"``, or ``None`` before ``identify()``.
        firmware: Always ``None``: the analyzer's program version is not readable.
        serial: Cached serial number, or ``None`` before ``identify()``.
        connected: Whether the session is operational.
        last_error: The context of the last failure, or ``None``.
        recoverable_error_count: Read retries that later succeeded, since open.
        captured_at: When the snapshot was taken (UTC, tz-aware).
    """

    name: str
    model: str | None
    firmware: str | None
    serial: str | None
    connected: bool
    last_error: ErrorContext | None
    recoverable_error_count: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class FujiDeviceSnapshot(DeviceSnapshot):
    """Analyzer-specific snapshot extras.

    Attributes:
        address: The station number, 1-31.
        protocol: The session's wire protocol.
        type_code: The raw type code, or ``None`` before ``identify()``.
        capabilities: Capabilities the session believes the analyzer has.
        availability: What each probe found, per capability.
        channels: The established channels, in order.
    """

    address: int
    protocol: ProtocolKind
    type_code: str | None
    capabilities: Capability
    availability: Mapping[Capability, Availability]
    channels: tuple[ChannelId, ...]
