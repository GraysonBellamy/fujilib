"""Channel identifiers, roles, gases and label provenance (design §2.9, §8).

The ZP-series presents up to **twelve display channels**: the measured
components first (NDIR, then O2), then O2-corrected values, corrected averages
and an O2 average. Only channels 1–5 carry per-channel status registers
(current range, calibration flags, errors, hold), so only they can be
*measured* channels; 6–12 are always derived.

Which gas a channel carries is **asserted by the caller**. The type code and
live data only *suggest* it, and every label records its
:class:`LabelSource` so a consumer can tell the two apart.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.errors import ErrorContext, FujiValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CHANNELS",
    "MEASURED_CHANNELS",
    "ChannelId",
    "ChannelRole",
    "Gas",
    "LabelSource",
    "coerce_channel",
    "coerce_channel_map",
    "coerce_gas",
]


class ChannelId(StrEnum):
    """A display channel, ``CH1`` … ``CH12``."""

    CH1 = "CH1"
    CH2 = "CH2"
    CH3 = "CH3"
    CH4 = "CH4"
    CH5 = "CH5"
    CH6 = "CH6"
    CH7 = "CH7"
    CH8 = "CH8"
    CH9 = "CH9"
    CH10 = "CH10"
    CH11 = "CH11"
    CH12 = "CH12"

    @property
    def number(self) -> int:
        """The 1-based channel number, as the manual counts."""
        return int(self.value[2:])

    @property
    def is_measured(self) -> bool:
        """Whether the channel has per-channel status registers (channels 1–5)."""
        return self.number <= len(MEASURED_CHANNELS)

    @classmethod
    def from_number(cls, number: int) -> ChannelId:
        """Return the channel with 1-based ``number``.

        Raises:
            FujiValidationError: ``number`` is not 1–12.
        """
        if not 1 <= number <= len(_ALL):
            msg = f"channel number must be 1-{len(_ALL)}, got {number}"
            raise FujiValidationError(msg, context=ErrorContext(channel=str(number)))
        return _ALL[number - 1]


_ALL: Final[tuple[ChannelId, ...]] = tuple(ChannelId)

#: All twelve display channels, in order.
CHANNELS: Final[tuple[ChannelId, ...]] = _ALL

#: The channels that can carry a measured component and have status registers.
MEASURED_CHANNELS: Final[tuple[ChannelId, ...]] = _ALL[:5]


class ChannelRole(StrEnum):
    """What a channel's value is (ZPA manual §5.3(3))."""

    INSTANTANEOUS = "instantaneous"
    """A measured component: an NDIR gas or O2."""

    O2_CORRECTED = "o2_corrected"
    """An NDIR value corrected to the O2 reference (type-code digit 21 = A or C)."""

    O2_CORRECTED_AVERAGE = "o2_corrected_average"
    """The moving average of an O2-corrected value (digit 21 = C)."""

    O2_AVERAGE = "o2_average"
    """The moving average of O2. The manual shows it but not its channel index."""

    UNKNOWN = "unknown"
    """Not established: no assertion and nothing the type code decodes."""


class Gas(StrEnum):
    """A measured gas.

    Values are lower-case formulas, which is the vocabulary ``capa``'s cone
    profile uses for its analyzers (``"o2"``, ``"co"``, ``"co2"``).
    """

    NO = "no"
    NOX = "nox"
    SO2 = "so2"
    CO2 = "co2"
    CO = "co"
    CH4 = "ch4"
    O2 = "o2"
    UNKNOWN = "unknown"


class LabelSource(StrEnum):
    """Where a channel's gas label came from (design §2.9)."""

    ASSERTED = "asserted"
    """Given by the caller in a ``channel_map``. The only source fit for calculation."""

    TYPE_CODE = "type_code"
    """Decoded from the analyzer's type code. A hint; the bench unit's code is stale."""

    INFERRED = "inferred"
    """From the layout rule (NDIR components first, then O2). A weaker hint."""

    UNKNOWN = "unknown"
    """No label."""


def coerce_channel(channel: ChannelId | str) -> ChannelId:
    """Resolve ``channel`` to a :class:`ChannelId`.

    Accepts a :class:`ChannelId` or a string such as ``"CH3"``, ``"ch3"`` or
    ``"3"``.

    Raises:
        FujiValidationError: ``channel`` names no channel.
    """
    if isinstance(channel, ChannelId):
        return channel
    text = channel.strip().upper()
    if text.isdigit():
        return ChannelId.from_number(int(text))
    try:
        return ChannelId(text)
    except ValueError:
        msg = f"unknown channel {channel!r}; expected CH1-CH12"
        raise FujiValidationError(msg, context=ErrorContext(channel=channel)) from None


def coerce_gas(gas: Gas | str) -> Gas:
    """Resolve ``gas`` to a :class:`Gas`, from a member or its formula in any case (``"O2"``).

    Raises:
        FujiValidationError: ``gas`` names no gas.
    """
    if isinstance(gas, Gas):
        return gas
    try:
        return Gas(gas.strip().lower())
    except ValueError:
        known = ", ".join(g.value for g in Gas if g is not Gas.UNKNOWN)
        msg = f"unknown gas {gas!r}; expected one of {known}"
        raise FujiValidationError(msg) from None


def coerce_channel_map(
    channel_map: Mapping[ChannelId | str, Gas | str],
) -> Mapping[ChannelId, Gas]:
    """Resolve a caller's channel map, as ``open_device`` and ``identify()`` take it.

    Raises:
        FujiValidationError: a channel or gas is unknown, a channel is asserted
            as ``unknown``, or one channel is named twice with different gases.
    """
    out: dict[ChannelId, Gas] = {}
    for key, value in channel_map.items():
        channel, gas = coerce_channel(key), coerce_gas(value)
        if gas is Gas.UNKNOWN:
            msg = f"{channel.value} cannot be asserted as unknown; leave it out instead"
            raise FujiValidationError(msg, context=ErrorContext(channel=channel.value))
        if out.setdefault(channel, gas) is not gas:
            msg = f"{channel.value} is asserted as both {out[channel].value} and {gas.value}"
            raise FujiValidationError(msg, context=ErrorContext(channel=channel.value))
    return MappingProxyType(dict(sorted(out.items(), key=lambda item: item[0].number)))
