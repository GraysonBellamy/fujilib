"""Enumerated register values, exactly as the manuals define them (design §2.6–§2.8).

Every member's value is the raw register value. Two traps are kept visible
rather than merged:

- **Alarm mode and alarm state are different enums.** Mode 2 is "high or low";
  state 2 is "low limit alarm", and state 0 is "no alarm".
- **The two "cycle unit" registers use different codes.** The auto-calibration,
  auto-zero and blowback cycles use 0 = hours, 1 = days
  (:class:`ScheduleCycleUnit`); the moving-average and measurement-point
  periods use 0 = hours, 1 = **minutes** (:class:`PeriodUnit`).

Real analyzers do not always stay inside the documented domains (the bench
unit's alarm-6 target channel reads 12, outside the documented 0–6), so
decoders use :func:`fujilib.protocol.modbus.codec.decode_enum`, which can keep
an undefined value as a plain ``int`` instead of failing a whole read.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag, StrEnum

__all__ = [
    "AlarmMode",
    "AlarmState",
    "CalibrationKind",
    "CalibrationRangeMode",
    "DayOfWeek",
    "DisplayScreen",
    "ErrorCode",
    "ErrorScope",
    "HoldMode",
    "KeyCode",
    "ManualCalibrationStep",
    "MeasurementPoint",
    "PeriodUnit",
    "RangeIndex",
    "RangeMethod",
    "ScheduleCycleUnit",
    "ZeroCalibrationMode",
]


class AlarmMode(IntEnum):
    """How an alarm is configured to trip (holding 40056–40060, 40131)."""

    HIGH = 0
    LOW = 1
    HIGH_OR_LOW = 2
    HIGH_HIGH = 3
    LOW_LOW = 4


class AlarmState(IntEnum):
    """Whether and how an alarm is currently tripped (input 30043–30047, 30191)."""

    NONE = 0
    HIGH = 1
    LOW = 2
    HIGH_HIGH = 3
    LOW_LOW = 4


class DayOfWeek(IntEnum):
    """Day of week in schedule start times (0 = Sunday … 6 = Saturday)."""

    SUNDAY = 0
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6


class ScheduleCycleUnit(IntEnum):
    """Unit of the auto-calibration, auto-zero and blowback cycles (0 h, 1 days)."""

    HOURS = 0
    DAYS = 1


class PeriodUnit(IntEnum):
    """Unit of the moving-average and measurement-point periods (0 h, 1 **min**)."""

    HOURS = 0
    MINUTES = 1


class RangeIndex(IntEnum):
    """A range as the registers encode it: 0 is range 1, 1 is range 2."""

    RANGE_1 = 0
    RANGE_2 = 1

    @property
    def number(self) -> int:
        """The 1-based range number."""
        return self.value + 1


class RangeMethod(IntEnum):
    """How a channel changes range (holding 40111–40115).

    ``AUTO`` switches up at 90 % of range 1 and back below 80 % (ZPA manual §6.1).
    """

    MANUAL = 0
    REMOTE = 1
    AUTO = 2


class HoldMode(IntEnum):
    """What the outputs hold during calibration (holding 40140)."""

    LAST_VALUE = 0
    SETTING = 1


class ZeroCalibrationMode(IntEnum):
    """Manual zero calibration scope (holding 40026–40030).

    ``AT_ONCE`` zeroes every channel so set when any one is zeroed at the panel,
    which widens a manual zero beyond the channel it was started on. Auto
    calibration and auto zero calibration ignore it: they zero every enabled
    channel together (ZPA manual p.47; design §2.6).
    """

    EACH = 0
    AT_ONCE = 1


class CalibrationRangeMode(IntEnum):
    """Which ranges a calibration adjusts (holding 40031–40035).

    ``BOTH`` calibrates both ranges together, manual or automatic, widening a
    calibration beyond the range it was started on (ZPA manual p.45; design §2.6).
    """

    CURRENT = 0
    BOTH = 1


class MeasurementPoint(IntEnum):
    """Measurement-point setting (holding 40157; option)."""

    LINE_1 = 0
    LINE_2 = 1
    SWITCHING = 2


class ErrorScope(StrEnum):
    """Whether an error concerns the whole analyzer or one channel."""

    ANALYZER = "analyzer"
    CHANNEL = "channel"


class ErrorCode(IntEnum):
    """Analyzer error numbers (ZPA manual §8, service manual §4; design §2.8).

    Errors 1, 2, 3 and 10 close the instrument-error (FAULT) contact; errors 4–9
    close the calibration-error contact and are reported per channel.
    """

    LIGHT_SOURCE = 1
    """Light source or sector motor fault."""
    DETECTOR = 2
    """Detector failure."""
    AD_CONVERSION = 3
    """A/D conversion fault."""
    ZERO_OUT_OF_RANGE = 4
    """Zero calibration outside the allowable range."""
    ZERO_AMOUNT_OVER_50 = 5
    """Zero calibration amount over 50 %FS. The panel offers a forced calibration."""
    SPAN_OUT_OF_RANGE = 6
    """Span calibration outside the allowable range."""
    SPAN_AMOUNT_OVER_50 = 7
    """Span calibration amount over 50 %FS. The panel offers a forced calibration."""
    UNSTABLE = 8
    """Reading unstable during calibration."""
    AUTO_CALIBRATION = 9
    """One of errors 4-8 occurred during auto calibration."""
    OUTPUT_CIRCUIT = 10
    """Output cable or DIO circuit fault."""

    @property
    def scope(self) -> ErrorScope:
        """Whether the error concerns the analyzer or a single channel."""
        return ErrorScope.CHANNEL if 4 <= self.value <= 9 else ErrorScope.ANALYZER  # noqa: PLR2004


class DisplayScreen(IntEnum):
    """The front panel's current screen (input 30181; TN5A1190a p.46).

    ``MAINTENANCE`` and ``FACTORY`` mean an operator is in the service menus.
    """

    MEASUREMENT = 0
    MENU = 1
    RANGE_CHANGE = 2
    CALIBRATION_SETTING = 3
    ALARM_SETTING = 4
    AUTO_CALIBRATION_SETTING = 5
    PEAK_ALARM_SETTING = 6
    PARAMETER_SETTING = 7
    MAINTENANCE = 8
    FACTORY = 9
    AUTO_ZERO_SETTING = 10


class ManualCalibrationStep(IntEnum):
    """The manual-calibration step shown on the panel (input 30182; TN5A1190a p.46).

    Values 1–3 are not defined by the manual.
    """

    NONE = 0
    ZERO_CHANNEL_SELECT = 4
    ZERO_WAIT = 5
    ZERO_RUNNING = 6
    SPAN_CHANNEL_SELECT = 7
    SPAN_WAIT = 8
    SPAN_RUNNING = 9
    ERROR_DISPLAY = 10


class CalibrationKind(IntEnum):
    """Range and kind of a calibration-log record (0 Z1, 1 S1, 2 Z2, 3 S2)."""

    ZERO_RANGE1 = 0
    SPAN_RANGE1 = 1
    ZERO_RANGE2 = 2
    SPAN_RANGE2 = 3

    @property
    def range(self) -> int:
        """The calibrated range, 1 or 2."""
        return 1 + self.value // 2

    @property
    def is_span(self) -> bool:
        """Whether this was a span (rather than zero) calibration."""
        return bool(self.value % 2)


class KeyCode(IntFlag):
    """Front-panel key codes of the key-simulation register 42001.

    **fujilib never writes these** (design §6.5): the same keys reach the
    factory menu. They exist so ``fuji-decode`` can explain a captured frame.
    """

    MODE = 0x01
    SIDE = 0x02
    UP = 0x04
    DOWN = 0x08
    ESC = 0x10
    ENT = 0x20
    ZERO = 0x40
    SPAN = 0x80
