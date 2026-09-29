"""The only definition of what fujilib may ever write (design §5.4).

:data:`WRITE_ENVELOPE` is frozen and **independent of the registry and of
probing**: nothing is derived from the register map, and no observation can
widen it. The Modbus client re-checks every write request against it as the
last step before ``anymodbus``, so a forged spec, a custom registry or an
address in a settings file cannot reach the wire outside it.

Deliberately excluded:

- ``00A4h``–``00ABh``, whose meaning (interference compensation coefficients)
  is inferred, not documented (design §2.6);
- ``07D0h``, key simulation, whose keys also reach the factory menu (design §6.5).

Inside the envelope, only :data:`REVIEWED_SETTINGS` are ever written: the
reviewed subset of design §5.4, by name and address, also written out here
independently of the registry. The registry refuses to mark anything else
writable, and a setting write is refused for anything not in it, so neither a
custom registry nor a forged spec can widen it.

The four operation commands are :class:`OperationSpec` entries, not registers:
each has its own facade method, safety tier and post-conditions, and none is
reachable by writing a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fujilib.devices.capability import Capability, SafetyTier
from fujilib.errors import ErrorContext, FujiConfigurationError, FujiValidationError
from fujilib.registry.regions import FC_WRITE_MULTIPLE, FC_WRITE_SINGLE

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "KEY_SIMULATION_ADDRESS",
    "OPERATIONS",
    "REVIEWED_SETTINGS",
    "WRITE_ENVELOPE",
    "OperationSpec",
    "WriteRange",
    "check_envelope",
    "envelope_allows",
]

#: The key-simulation register 42001. fujilib never writes it (design §6.5).
KEY_SIMULATION_ADDRESS: Final = 0x07D0


@dataclass(frozen=True, slots=True)
class WriteRange:
    """Addresses ``first``..``last`` (inclusive) that function code ``fc`` may write."""

    fc: int
    first: int
    last: int

    def contains(self, fc: int, address: int, count: int = 1) -> bool:
        """Whether a write of ``count`` words at ``address`` with ``fc`` lies inside."""
        return (
            fc == self.fc
            and count >= 1
            and self.first <= address <= address + count - 1 <= self.last
        )


#: Everything fujilib may ever write. Frozen; see the module docstring.
WRITE_ENVELOPE: Final[tuple[WriteRange, ...]] = (
    WriteRange(fc=FC_WRITE_SINGLE, first=0x0000, last=0x009D),
    WriteRange(fc=FC_WRITE_MULTIPLE, first=0x0000, last=0x00A3),
    WriteRange(fc=FC_WRITE_SINGLE, first=0x07D1, last=0x07D4),  # operation commands 42002-42005
)


_CHANNELS: Final = range(1, 6)

#: The settings fujilib writes (design §5.4), as ``(name, holding address)``.
REVIEWED_SETTINGS: Final[frozenset[tuple[str, int]]] = frozenset(
    {
        *(
            (f"calibration_gas.ch{c}.range{r}.{kind}", 4 * (c - 1) + 2 * (r - 1) + k)
            for c in _CHANNELS
            for r in (1, 2)
            for k, kind in enumerate(("zero", "span"))
        ),
        *((f"calibration.ch{c}.zero_mode", 0x19 + c - 1) for c in _CHANNELS),
        *((f"calibration.ch{c}.range_mode", 0x1E + c - 1) for c in _CHANNELS),
        *((f"response_time.ndir{k}", 0x4B + 2 * (k - 1)) for k in range(1, 5)),
        ("response_time.o2", 0x53),
        ("output_hold.enabled", 0x5C),
        *((f"range.ch{c}.selected", 0x69 + c - 1) for c in _CHANNELS),
        *((f"range.ch{c}.method", 0x6E + c - 1) for c in _CHANNELS),
        ("hold.mode", 0x8B),
        *((f"hold.ch{c}.value", 0x8C + c - 1) for c in _CHANNELS),
    }
)


def envelope_allows(fc: int, address: int, count: int = 1) -> bool:
    """Whether a ``count``-word write at ``address`` with ``fc`` lies inside one envelope range."""
    return any(r.contains(fc, address, count) for r in WRITE_ENVELOPE)


def check_envelope(fc: int, address: int, count: int = 1) -> None:
    """Refuse a write outside :data:`WRITE_ENVELOPE`.

    Raises:
        FujiValidationError: the write is outside the envelope.
    """
    if not envelope_allows(fc, address, count):
        msg = (
            f"FC{fc:02X} write of {count} word(s) at 0x{address:04X} is outside the write envelope"
        )
        raise FujiValidationError(
            msg,
            context=ErrorContext(
                function_code=fc, register_address=address, extra={"count": count}
            ),
        )


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """One operation command: write ``value`` to ``address`` with FC06 (design §2.7)."""

    name: str
    address: int
    value: int
    safety: SafetyTier
    requires: Capability
    effect: str
    manual_ref: str

    @property
    def register_number(self) -> int:
        """The 40001-based register number, for humans and docs."""
        return 40_001 + self.address


#: The documented operation commands, by facade name. Key simulation is not one.
OPERATIONS: Final[Mapping[str, OperationSpec]] = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            OperationSpec(
                "return_to_measurement",
                0x07D1,
                1,
                SafetyTier.STATEFUL,
                Capability.NONE,
                "Force the display back to measurement mode.",
                "TN5A1190a p.33",
            ),
            OperationSpec(
                "start_auto_calibration",
                0x07D2,
                1,
                SafetyTier.DANGEROUS,
                Capability.AUTO_CALIBRATION,
                "Run auto calibration once. There is no register to stop it.",
                "TN5A1190a p.33",
            ),
            OperationSpec(
                "start_auto_zero_calibration",
                0x07D3,
                1,
                SafetyTier.DANGEROUS,
                Capability.AUTO_ZERO,
                "Run auto zero calibration once. There is no register to stop it.",
                "TN5A1190a p.33",
            ),
            OperationSpec(
                "start_blowback",
                0x07D4,
                1,
                SafetyTier.STATEFUL,
                Capability.BLOWBACK,
                "Run blowback once.",
                "TN5A1190a p.33",
            ),
        )
    }
)


def _check_operations() -> None:
    for spec in OPERATIONS.values():
        if not envelope_allows(FC_WRITE_SINGLE, spec.address):  # pragma: no cover — tested
            msg = f"operation {spec.name!r} at 0x{spec.address:04X} is outside the write envelope"
            raise FujiConfigurationError(msg)


_check_operations()
