"""Safety tiers, capability flags and probe availability (design §6.2, §6.6).

- :class:`SafetyTier` — how dangerous an operation is; anything above
  ``READ_ONLY`` needs ``confirm=True``.
- :class:`Capability` — firmware, option and model features.
- :class:`Availability` — what a probe found for one capability.

This module is a **leaf**: it imports nothing from :mod:`fujilib`, so the
registry can use these enums without an import cycle. Note that ``anymodbus``
also exports a ``Capability``; never import that one bare beside this one.
"""

from __future__ import annotations

from enum import Flag, IntEnum, StrEnum, auto
from typing import Final

__all__ = ["PROBED_CAPABILITIES", "Availability", "Capability", "SafetyTier"]


class SafetyTier(IntEnum):
    """How dangerous an operation is. The tier follows its **effect** (design §6.2)."""

    READ_ONLY = 0
    """Every read."""

    STATEFUL = 1
    """A transient state change: return to measurement, blowback. Requires ``confirm=True``."""

    PERSISTENT = 2
    """A settings write that changes only configuration. Requires ``confirm=True``."""

    DANGEROUS = 3
    """Calibration, calibration schedules, calibration gases and scope.

    Requires ``confirm=True``, and the CLI also requires
    ``--i-understand-this-is-destructive``.
    """


class Capability(Flag):
    """Features that depend on firmware, options or model.

    The first group is **probed** by ``identify()`` and never assumed
    (design §6.6). The rest gate writes to option registers; they do not gate
    reads, because an option's registers read even when it is not fitted.
    """

    NONE = 0

    CLOCK = auto()
    """Undocumented real-time clock at FC04 03E8h (observed on the bench unit)."""
    ADC_VALUES = auto()
    """Undocumented A/D values at FC04 03EFh (observed on the bench unit)."""
    TYPE_CODE_EXT = auto()
    """Type-code digits 27-29 at FC04 047Ah; firmware 2.24 or later."""
    CALIBRATION_LOG = auto()
    """Calibration log at FC04 1000h; firmware 2.24 or later."""

    ALARMS = auto()
    """Concentration alarms 1-6 (option)."""
    AUTO_CALIBRATION = auto()
    """Auto calibration (option)."""
    AUTO_ZERO = auto()
    """Auto zero calibration (option)."""
    AVERAGING = auto()
    """Moving-average outputs (option)."""
    O2_CORRECTION = auto()
    """O2-corrected outputs (type-code digit 21)."""
    BLOWBACK = auto()
    """Blowback (option)."""
    MEASUREMENT_POINT = auto()
    """Measurement-point switching (option)."""
    REFERENCE_GAS = auto()
    """Reference-gas switching and averaging: ZPB and ZPG only."""


#: The capabilities ``identify()`` probes, in probe order (design §6.6).
PROBED_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability.CLOCK,
    Capability.ADC_VALUES,
    Capability.TYPE_CODE_EXT,
    Capability.CALIBRATION_LOG,
)


class Availability(StrEnum):
    """What a probe found for one capability (design §6.6).

    Only ``UNSUPPORTED`` short-circuits later calls; ``UNKNOWN`` is retried on
    next use.
    """

    UNKNOWN = "unknown"
    """Not probed yet, or the probe timed out or failed framing."""

    SUPPORTED = "supported"
    """The probe returned data that validates."""

    UNSUPPORTED = "unsupported"
    """A well-formed probe inside one known block was answered with exception 02."""

    INVALID_DATA = "invalid_data"
    """Readable, but the content does not validate (e.g. a clock that is not a date)."""
