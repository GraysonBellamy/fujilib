"""Device profiles: what one analyzer family brings (design §5.3).

A :class:`DeviceProfile` names a family and carries what differs between
families: its register map, its protocol and serial framing, how a station is
identified, and how discovery recognizes one. Profiles are shared and frozen;
what one station turns out to be (its channels, what its probes found) lives
in the session, never in the profile.

One profile exists: :data:`ZP_PROFILE`, for the ZPA and the ZPB, ZPG, ZPAJ and
ZPG3E models that share its MODBUS map (TN5A1190).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from fujilib.devices.reads import read_identity
from fujilib.errors import FujiDecodeError
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.codec import decode_chars
from fujilib.protocol.modbus.read_plan import DISCOVERY_PROBE
from fujilib.registry.registers import REGISTRY
from fujilib.transport.base import SerialSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fujilib._deadline import Deadline
    from fujilib.devices.reads import Identity
    from fujilib.protocol.base import ProtocolClient
    from fujilib.protocol.modbus.read_plan import BlockRead
    from fujilib.registry.registers import RegisterRegistry

__all__ = ["DEVICE_PROFILES", "ZP_PROFILE", "DeviceProfile", "IdentifyStrategy", "recognize_zp"]


class IdentifyStrategy(Protocol):
    """Reads what ``identify()`` establishes about one station."""

    async def __call__(
        self, client: ProtocolClient, *, probe: bool = True, deadline: Deadline | None = None
    ) -> Identity:
        """Read the station's identity, ranges and readings, and probe its capabilities."""
        ...


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """One analyzer family."""

    name: str
    """A short name, e.g. ``"zp"``."""
    registry: RegisterRegistry
    """The family's register map; parameter names resolve against it."""
    default_protocol: ProtocolKind
    default_serial: SerialSettings
    """The family's serial framing. Its ``port`` is a placeholder the caller's port replaces."""
    identify: IdentifyStrategy
    discovery_probe: BlockRead
    """The one read discovery sends to each station."""
    recognize: Callable[[Sequence[int]], str | None]
    """The model the probe's words name, or ``None`` when they are not this family's."""


def recognize_zp(words: Sequence[int]) -> str | None:
    """The model when type-code digits 1-3 read ``Z``, ``P`` and a model letter (design §7.5).

    Returns ``"ZPA"``, ``"ZPB"``, ``"ZPG"``… or ``None`` for anything else.
    """
    try:
        text = decode_chars(words, strip=False)
    except FujiDecodeError:
        return None
    if len(text) == DISCOVERY_PROBE.count and text.startswith("ZP") and "A" <= text[-1] <= "Z":
        return text
    return None


#: The ZP series: ZPA, ZPB, ZPG (RS-485) and ZPAJ, ZPG3E (RS-232C), 38400 8-N-1.
ZP_PROFILE: Final = DeviceProfile(
    name="zp",
    registry=REGISTRY,
    default_protocol=ProtocolKind.MODBUS_RTU,
    default_serial=SerialSettings(port=""),
    identify=read_identity,
    discovery_probe=DISCOVERY_PROBE,
    recognize=recognize_zp,
)

#: Every profile discovery tries, in order.
DEVICE_PROFILES: Final[tuple[DeviceProfile, ...]] = (ZP_PROFILE,)
