"""Blocking discovery (design §7.5)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

from fujilib.devices.discovery import find_devices as _find_devices
from fujilib.devices.profile import DEVICE_PROFILES
from fujilib.sync.portal import SyncPortal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib.devices.discovery import DiscoveryResult
    from fujilib.devices.profile import DeviceProfile

__all__ = ["find_devices"]


def find_devices(
    *,
    ports: Sequence[str] | None = None,
    addresses: Sequence[int] = (1,),
    profiles: Sequence[DeviceProfile] = DEVICE_PROFILES,
    per_probe_timeout_s: float = 0.3,
    identify: bool = True,
    max_concurrency: int = 8,
    portal: SyncPortal | None = None,
) -> list[DiscoveryResult]:
    """Blocking :func:`fujilib.devices.discovery.find_devices`.

    Runs on ``portal``, or on a portal of its own when ``portal`` is ``None``.
    """
    with SyncPortal() if portal is None else nullcontext(portal) as active:
        return active.call(
            _find_devices,
            ports=ports,
            addresses=addresses,
            profiles=profiles,
            per_probe_timeout_s=per_probe_timeout_s,
            identify=identify,
            max_concurrency=max_concurrency,
        )
