"""The :class:`ProtocolKind` enum.

``ProtocolKind`` has the ``Kind`` suffix to avoid colliding with
:class:`typing.Protocol` at import sites. It has one member today: the ZP
series speaks only MODBUS RTU. It is kept for family harmony and for the
``protocol`` column in rows (design §7.1, §13.1 #6).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ProtocolKind"]


class ProtocolKind(StrEnum):
    """The wire protocol of a session."""

    MODBUS_RTU = "modbus_rtu"
