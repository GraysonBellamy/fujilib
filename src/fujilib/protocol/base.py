"""The :class:`ProtocolKind` enum and the :class:`ProtocolClient` contract.

``ProtocolKind`` has the ``Kind`` suffix to avoid colliding with
:class:`typing.Protocol` at import sites. It has one member today: the ZP
series speaks only MODBUS RTU. It is kept for family harmony and for the
``protocol`` column in rows (design §7.1, §13.1 #6).

:class:`ProtocolClient` is what the device layer needs from a station's
client: block reads with their timing, the two write primitives, and the
traffic counters. :class:`~fujilib.protocol.modbus.client.ModbusClient` is
the implementation; the read functions in :mod:`fujilib.devices.reads` accept
anything that satisfies it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib._deadline import Deadline
    from fujilib.devices.models import TransferTiming
    from fujilib.protocol.modbus.client import BlockReply, ClientCounters, PlanReply
    from fujilib.protocol.modbus.read_plan import BlockRead

__all__ = ["ProtocolClient", "ProtocolKind"]


class ProtocolKind(StrEnum):
    """The wire protocol of a session."""

    MODBUS_RTU = "modbus_rtu"


class ProtocolClient(Protocol):
    """One station's client, as the device layer uses it."""

    @property
    def address(self) -> int:
        """The station number."""
        ...

    @property
    def label(self) -> str:
        """The port's canonical name."""
        ...

    @property
    def counters(self) -> ClientCounters:
        """The station's traffic counters."""
        ...

    @property
    def recoverable_error_count(self) -> int:
        """Failed read attempts that a retry recovered."""
        ...

    async def read(
        self, block: BlockRead, *, deadline: Deadline | None = None, command: str = "read"
    ) -> BlockReply:
        """Read one block."""
        ...

    async def read_plan(
        self,
        plan: Sequence[BlockRead],
        *,
        deadline: Deadline | None = None,
        command: str = "read",
    ) -> PlanReply:
        """Read every block of ``plan`` without other traffic in between."""
        ...

    async def write_register(
        self,
        address: int,
        value: int,
        *,
        deadline: Deadline | None = None,
        command: str = "write_register",
    ) -> TransferTiming:
        """FC06: write one word inside the write envelope. Never retried."""
        ...

    async def write_registers(
        self,
        address: int,
        values: Sequence[int],
        *,
        deadline: Deadline | None = None,
        command: str = "write_registers",
    ) -> TransferTiming:
        """FC10: write consecutive words inside the write envelope. Never retried."""
        ...
