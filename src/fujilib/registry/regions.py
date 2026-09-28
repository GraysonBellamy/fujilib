"""Valid address regions per function code (design §2.3).

A block read may bridge unused addresses *inside* a region but must never
cross a region boundary: the bench unit answers a read that crosses a region's
end with exception 03, and one that starts outside the map with exception 02
(design §2.2). The planner therefore closes a block at every region change.

Regions are listed per function code, because FC03 and FC04 address separate
tables and FC06 covers a smaller holding range than FC10. Two kinds are kept
apart:

- **documented** regions come from the MODBUS manual (TN5A1190a);
- **observed** regions were found on the bench unit and are only used for
  registers behind a probed :class:`~fujilib.devices.capability.Capability`.

A :class:`RegionMap` lists the regions fujilib reads from, not the whole
readable map; the analyzer also answers inside blocks fujilib never touches
(protocol findings §4). Being in a region never makes a register writable: the
write envelope is a separate frozen constant
(:mod:`fujilib.registry.write_policy`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Final

from fujilib.devices.capability import Capability
from fujilib.errors import ErrorContext, FujiConfigurationError

__all__ = [
    "DOCUMENTED_REGIONS",
    "FC_READ_HOLDING",
    "FC_READ_INPUT",
    "FC_WRITE_MULTIPLE",
    "FC_WRITE_SINGLE",
    "OBSERVED_REGIONS",
    "ZP_REGIONS",
    "Evidence",
    "Region",
    "RegionMap",
    "RegisterTable",
]

FC_READ_HOLDING: Final = 0x03
FC_READ_INPUT: Final = 0x04
FC_WRITE_SINGLE: Final = 0x06
FC_WRITE_MULTIPLE: Final = 0x10


class RegisterTable(StrEnum):
    """The two register tables: holding (4xxxx) and input (3xxxx)."""

    HOLDING = "holding"
    INPUT = "input"

    @property
    def read_function(self) -> int:
        """The function code that reads this table."""
        return FC_READ_HOLDING if self is RegisterTable.HOLDING else FC_READ_INPUT

    @property
    def number_base(self) -> int:
        """The register-number base: 40001 for holding, 30001 for input."""
        return 40_001 if self is RegisterTable.HOLDING else 30_001


class Evidence(StrEnum):
    """How much a fact about a register or region is worth (design §5.1)."""

    DOCUMENTED = "documented"
    """Stated by the manuals."""

    OBSERVED = "observed"
    """Seen on the bench unit (firmware 1.02) but not documented."""

    INFERRED = "inferred"
    """An interpretation of what was observed or documented. Never writable."""

    CONTESTED = "contested"
    """Documented, but the bench unit contradicts the manual. Kept raw; never writable."""


@dataclass(frozen=True, slots=True)
class Region:
    """A contiguous run of addresses one function code may access."""

    name: str
    function: int
    first: int
    last: int
    evidence: Evidence
    doc: str
    requires: Capability = Capability.NONE

    def __post_init__(self) -> None:
        if self.first > self.last:
            msg = f"region {self.name!r} ends before it starts"
            raise FujiConfigurationError(msg)

    @property
    def count(self) -> int:
        """Number of addresses in the region."""
        return self.last - self.first + 1

    def contains(self, address: int, count: int = 1) -> bool:
        """Whether ``count`` addresses from ``address`` all lie in this region."""
        return count >= 1 and self.first <= address and address + count - 1 <= self.last


@dataclass(frozen=True, slots=True)
class RegionMap:
    """The regions of one analyzer or profile, per function code.

    Regions of the same function code must not overlap.
    """

    regions: tuple[Region, ...]

    def __post_init__(self) -> None:
        by_fc: dict[int, list[Region]] = {}
        for region in self.regions:
            by_fc.setdefault(region.function, []).append(region)
        for fc, regions in by_fc.items():
            ordered = sorted(regions, key=lambda r: r.first)
            for before, after in pairwise(ordered):
                if after.first <= before.last:
                    msg = f"regions {before.name!r} and {after.name!r} overlap for FC{fc:02X}"
                    raise FujiConfigurationError(
                        msg, context=ErrorContext(function_code=fc, register_address=after.first)
                    )

    def for_function(self, function: int) -> tuple[Region, ...]:
        """The regions of ``function``, in address order."""
        return tuple(
            sorted((r for r in self.regions if r.function == function), key=lambda r: r.first)
        )

    def region_for(self, function: int, address: int, count: int = 1) -> Region | None:
        """The region that holds all ``count`` addresses from ``address``, if one does."""
        for region in self.regions:
            if region.function == function and region.contains(address, count):
                return region
        return None

    def with_regions(self, *extra: Region) -> RegionMap:
        """Return a new map with ``extra`` regions added (for per-station observations)."""
        return RegionMap((*self.regions, *extra))


#: The regions of the MODBUS manual (TN5A1190a p.19-24, p.38-40).
DOCUMENTED_REGIONS: Final = RegionMap(
    (
        Region(
            "user_settings",
            FC_READ_HOLDING,
            0x0000,
            0x00AB,
            Evidence.DOCUMENTED,
            "User settings (40001-40172).",
        ),
        Region(
            "user_settings",
            FC_WRITE_MULTIPLE,
            0x0000,
            0x00AB,
            Evidence.DOCUMENTED,
            "User settings, written with FC10 (40001-40172).",
        ),
        Region(
            "user_settings",
            FC_WRITE_SINGLE,
            0x0000,
            0x009D,
            Evidence.DOCUMENTED,
            "User settings, written with FC06 (40001-40158); the FC06 bound is below FC10's.",
        ),
        Region(
            "commands",
            FC_WRITE_SINGLE,
            0x07D0,
            0x07D4,
            Evidence.DOCUMENTED,
            "Operation commands (42001-42005), write-only.",
        ),
        Region(
            "measurement",
            FC_READ_INPUT,
            0x0000,
            0x00C1,
            Evidence.DOCUMENTED,
            "Measurements and status (30001-30194).",
        ),
        Region(
            "fixed_settings",
            FC_READ_INPUT,
            0x0425,
            0x0469,
            Evidence.DOCUMENTED,
            "Ranges, units, type code and serial number (31062-31130).",
        ),
        Region(
            "type_code_ext",
            FC_READ_INPUT,
            0x047A,
            0x047C,
            Evidence.DOCUMENTED,
            "Type-code digits 27-29 (31147-31149), firmware 2.24 or later.",
            Capability.TYPE_CODE_EXT,
        ),
        Region(
            "calibration_log",
            FC_READ_INPUT,
            0x1000,
            0x1707,
            Evidence.DOCUMENTED,
            "Calibration log (34097-35896), firmware 2.24 or later.",
            Capability.CALIBRATION_LOG,
        ),
    )
)

#: Regions seen on the bench unit that hold registers fujilib models (findings §4.1).
OBSERVED_REGIONS: Final = RegionMap(
    (
        Region(
            "service",
            FC_READ_INPUT,
            0x03E8,
            0x0418,
            Evidence.OBSERVED,
            "Undocumented real-time clock and A/D values.",
        ),
    )
)

#: The read and write regions of the ZP-series profile.
ZP_REGIONS: Final = DOCUMENTED_REGIONS.with_regions(*OBSERVED_REGIONS.regions)
