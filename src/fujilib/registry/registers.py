"""The ZP-series register map: :class:`RegisterSpec` and the registry (design §5.1).

The map is written in Python, generated from the manual's stride patterns
(``for c in 1..5``, ``for r in 1..2``), so it reads like the manual's tables
and mypy checks it. It is the single source of truth for addresses, allowed
function codes, value domains, scaling, safety tiers and evidence;
``docs/registers.md`` is generated from it.

Addresses are 0-based relative addresses, exactly as on the wire. Page
references are PDF page numbers of the manuals, which match the page markers
of the text extracts (the printed page numbers differ).

**What is writable.** A holding register is writable only when its
declaration gives it a write tier. That is the reviewed subset of design §5.4:
documented, not contradicted by the bench unit, not an option, and testable on
the bench analyzer. Every other register, holding or input, is read-only. For
a writable register ``minimum`` and ``maximum`` are the limits of a write, the
narrower of the two manuals' where they disagree (the MODBUS manual defers
setting ranges to the instruction manual, TN5A1190a p.28).

The whole map is validated at import (:func:`validate_map`) and a violation
fails loudly as :class:`~fujilib.errors.FujiConfigurationError`:

- names are unique;
- no two entries overlap within a table;
- every entry lies inside a region for each function code it lists;
- every writable entry lies inside the frozen write envelope, is one word,
  can be written with FC06, and is one of the reviewed settings of
  :data:`~fujilib.registry.write_policy.REVIEWED_SETTINGS`;
- only documented registers are writable, and only above ``READ_ONLY``;
- enum, limit and scaling metadata are consistent.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, NoReturn

from fujilib.devices.capability import Capability, SafetyTier
from fujilib.errors import ErrorContext, FujiConfigurationError, FujiValidationError
from fujilib.protocol.modbus.codec import MAX_DECIMALS, DataType
from fujilib.registry.channels import ChannelId
from fujilib.registry.enums import (
    AlarmMode,
    AlarmState,
    CalibrationKind,
    CalibrationRangeMode,
    DayOfWeek,
    DisplayScreen,
    HoldMode,
    ManualCalibrationStep,
    MeasurementPoint,
    PeriodUnit,
    RangeIndex,
    RangeMethod,
    ScheduleCycleUnit,
    ZeroCalibrationMode,
)
from fujilib.registry.regions import (
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    ZP_REGIONS,
    Evidence,
    RegionMap,
    RegisterTable,
)
from fujilib.registry.write_policy import REVIEWED_SETTINGS, envelope_allows

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

__all__ = [
    "REGISTRY",
    "Access",
    "LogField",
    "LogSpec",
    "RegisterRegistry",
    "RegisterSpec",
    "Scaling",
    "ScalingKind",
    "validate_map",
]


class Access(StrEnum):
    """Whether fujilib may write a register. Operation commands are not registers."""

    READ = "read"
    READ_WRITE = "read_write"


class ScalingKind(StrEnum):
    """Where a register's decimal-point position (and concentration unit) comes from."""

    NONE = "none"
    """An unscaled count, switch or time."""

    FIXED = "fixed"
    """A constant number of decimals (the calibration deviation is %FS x 10)."""

    INLINE = "inline"
    """The two registers that follow it, read in the same block (concentrations)."""

    BY_RANGE = "by_range"
    """The (channel, range) decimal-point and unit registers, 31087-31096 and 31067-31076."""

    BY_ALARM_TARGET = "by_alarm_target"
    """The range registers of the alarm's target channel, whose encoding is contested."""


@dataclass(frozen=True, slots=True)
class Scaling:
    """A scaling rule. The concentration unit comes from the same place as the decimals."""

    kind: ScalingKind
    decimals: int | None = None

    def __str__(self) -> str:
        if self.kind is ScalingKind.FIXED:
            return f"fixed({self.decimals})"
        return self.kind.value


_NONE: Final = Scaling(ScalingKind.NONE)
_INLINE: Final = Scaling(ScalingKind.INLINE)
_BY_RANGE: Final = Scaling(ScalingKind.BY_RANGE)
_BY_ALARM: Final = Scaling(ScalingKind.BY_ALARM_TARGET)


@dataclass(frozen=True, slots=True)
class RegisterSpec:
    """One register (or a multi-word value) of the map.

    ``minimum`` and ``maximum`` are raw limits, before scaling; for a writable
    register they are the limits of a write. ``unit`` is a fixed display unit
    (``"s"``, ``"%FS"``); a scaled concentration takes its unit from the same
    source as its decimals. ``notes`` records where the manuals disagree or the
    bench unit contradicts them.
    """

    name: str
    group: str
    table: RegisterTable
    address: int
    dtype: DataType
    access: Access
    read_functions: frozenset[int]
    write_functions: frozenset[int]
    safety: SafetyTier
    evidence: Evidence
    manual_ref: str
    doc: str
    count: int = 1
    scaling: Scaling = _NONE
    unit: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    write_values: frozenset[int] | None = None
    """The raw values a write may use, where fewer than the limits allow."""
    write_percent_fs: tuple[int, int] | None = None
    """For a range-scaled setting: the limits of a write, in percent of the range's full scale."""
    enum: type[IntEnum] | None = None
    channel: ChannelId | None = None
    range: int | None = None
    alarm: int | None = None
    requires: Capability = Capability.NONE
    notes: str = ""

    @property
    def register_number(self) -> int:
        """The 40001- or 30001-based register number, for humans and docs."""
        return self.table.number_base + self.address

    @property
    def last_address(self) -> int:
        """The last address the value occupies."""
        return self.address + self.count - 1

    @property
    def writable(self) -> bool:
        """Whether fujilib may ever write this register."""
        return self.access is Access.READ_WRITE


@dataclass(frozen=True, slots=True)
class LogField:
    """One field of a log record, at ``offset`` words from the record start."""

    name: str
    offset: int
    dtype: DataType
    doc: str
    count: int = 1
    enum: type[IntEnum] | None = None
    scaling: Scaling = _NONE


@dataclass(frozen=True, slots=True)
class LogSpec:
    """A log of fixed-width records, newest first (design §2.6).

    ``channels`` regions of ``records`` records each start ``channel_stride``
    words apart; record ``i`` (0 = newest) of channel ``c`` starts at
    ``base + channel_stride * (c - 1) + record_words * i``.
    """

    name: str
    group: str
    table: RegisterTable
    base: int
    record_words: int
    records: int
    fields: tuple[LogField, ...]
    evidence: Evidence
    manual_ref: str
    doc: str
    channels: int = 1
    channel_stride: int = 0
    requires: Capability = Capability.NONE
    notes: str = ""

    @property
    def last_address(self) -> int:
        """The last address of the whole log."""
        return self.record_address(self.channels, self.records - 1) + self.record_words - 1

    @property
    def words_per_channel(self) -> int:
        """Words in one channel's region."""
        return self.records * self.record_words

    def record_address(self, channel: int, index: int) -> int:
        """First address of record ``index`` (0 = newest) of ``channel`` (1-based).

        Raises:
            FujiValidationError: ``channel`` or ``index`` is out of range.
        """
        if not 1 <= channel <= self.channels or not 0 <= index < self.records:
            msg = f"{self.name}: no record {index} for channel {channel}"
            raise FujiValidationError(msg)
        return self.base + self.channel_stride * (channel - 1) + self.record_words * index


# --- Declaration helpers ---------------------------------------------------


def _read_functions(table: RegisterTable) -> frozenset[int]:
    return frozenset({table.read_function})


#: A writable setting is one word inside FC06's reach, so either function writes it.
_WRITE_FUNCTIONS: Final = frozenset({FC_WRITE_SINGLE, FC_WRITE_MULTIPLE})


def _spec(
    table: RegisterTable,
    name: str,
    group: str,
    address: int,
    *,
    ref: str,
    doc: str,
    write: SafetyTier | None = None,
    dtype: DataType = DataType.UINT16,
    evidence: Evidence = Evidence.DOCUMENTED,
    count: int = 1,
    scaling: Scaling = _NONE,
    unit: str | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    write_values: frozenset[int] | None = None,
    write_percent_fs: tuple[int, int] | None = None,
    enum: type[IntEnum] | None = None,
    channel: ChannelId | None = None,
    rng: int | None = None,
    alarm: int | None = None,
    requires: Capability = Capability.NONE,
    notes: str = "",
) -> RegisterSpec:
    """Build a spec. ``enum`` implies an ENUM type and its limits; a BOOL is 0..1.

    ``write`` makes the register writable at that safety tier; without it the
    register is read-only.
    """
    if enum is not None:
        dtype = DataType.ENUM
        minimum, maximum = min(enum), max(enum)
    elif dtype is DataType.BOOL:
        minimum, maximum = 0, 1
    return RegisterSpec(
        name=name,
        group=group,
        table=table,
        address=address,
        count=count,
        dtype=dtype,
        access=Access.READ_WRITE if write is not None else Access.READ,
        read_functions=_read_functions(table),
        write_functions=_WRITE_FUNCTIONS if write is not None else frozenset(),
        safety=write if write is not None else SafetyTier.READ_ONLY,
        evidence=evidence,
        manual_ref=ref,
        doc=doc,
        scaling=scaling,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        write_values=write_values,
        write_percent_fs=write_percent_fs,
        enum=enum,
        channel=channel,
        range=rng,
        alarm=alarm,
        requires=requires,
        notes=notes,
    )


def _setting(name: str, group: str, address: int, **kw: Any) -> RegisterSpec:
    """A holding register (see :func:`_spec`)."""
    return _spec(RegisterTable.HOLDING, name, group, address, **kw)


def _input(name: str, group: str, address: int, **kw: Any) -> RegisterSpec:
    """An input register: always read-only (see :func:`_spec`)."""
    return _spec(RegisterTable.INPUT, name, group, address, **kw)


def _ch(number: int) -> ChannelId:
    return ChannelId.from_number(number)


_CHANNELS_5: Final = range(1, 6)
_RANGES: Final = (1, 2)
_ALARMS_5: Final = range(1, 6)
_LIMIT_KINDS: Final = ("high", "low")

_CONTESTED_BCD: Final = (
    "The manual says BCD (00h-23h / 00h-59h). The bench unit reads 0x000C, which is "
    "not BCD, in all three schedules, so the encoding is unconfirmed and the value is "
    "kept raw (design §2.6)."
)
_NO_CYCLE_LIMITS: Final = (
    "No limits in the MODBUS manual; the ZPA manual gives 1-99 h or 1-40 days (ZPA p.55, p.93)."
)
_BY_ALARM_TARGET_NOTE: Final = (
    "Scaled by the range of the alarm's target channel; the target encoding is contested."
)
_ALARM_LIMIT_NOTE: Final = (
    "The ZPA manual limits them to 0-100 %FS, with the high limit above the low one by more "
    "than the hysteresis, and 0 meaning no alarm (ZPA p.48)."
)


# --- Holding registers ------------------------------------------------------


_GAS_LIMITS: Final = {"zero": (0, 100), "span": (1, 105)}
_GAS_NOTE: Final = (
    "Takes effect at the next calibration, manual or automatic (ZPA p.42). Writes are "
    "limited to 0-100 %FS for zero gas and 1-105 %FS for span gas: the ZPA manual gives "
    "1-105 %FS for span gas and no zero-gas limit for NDIR or built-in O2 (ZPA p.42). "
    "Its separate limits for external zirconia and reverse-range O2 are not modelled, so "
    "some legal values there are refused."
)


def _calibration_settings() -> Iterator[RegisterSpec]:
    for c in _CHANNELS_5:
        for r in _RANGES:
            for k, kind in enumerate(("zero", "span")):
                yield _setting(
                    f"calibration_gas.ch{c}.range{r}.{kind}",
                    "Calibration gas",
                    4 * (c - 1) + 2 * (r - 1) + k,
                    write=SafetyTier.DANGEROUS,
                    ref="TN5A1190a p.28",
                    doc=f"Ch{c} range {r} {kind} calibration gas concentration.",
                    minimum=0,
                    maximum=9999,
                    write_percent_fs=_GAS_LIMITS[kind],
                    scaling=_BY_RANGE,
                    channel=_ch(c),
                    rng=r,
                    notes=_GAS_NOTE,
                )
    for c in _CHANNELS_5:
        yield _setting(
            f"auto_calibration.ch{c}.included",
            "Auto calibration",
            0x14 + c - 1,
            ref="TN5A1190a p.29",
            doc=f"Whether auto calibration and auto zero calibration calibrate Ch{c}.",
            channel=_ch(c),
            requires=Capability.AUTO_CALIBRATION,
            dtype=DataType.BOOL,
            notes=(
                "The same list chooses the channels of auto zero calibration (ZPA p.59). "
                "Their zero is done together, their span one after another from Ch1 (ZPA p.47)."
            ),
        )
    for c in _CHANNELS_5:
        yield _setting(
            f"calibration.ch{c}.zero_mode",
            "Calibration scope",
            0x19 + c - 1,
            write=SafetyTier.DANGEROUS,
            ref="TN5A1190a p.29",
            doc=f"Ch{c} manual zero at the panel: alone, or with every 'at once' channel.",
            channel=_ch(c),
            enum=ZeroCalibrationMode,
            notes=(
                "Affects only a manual zero started at the panel. Auto calibration and auto "
                "zero calibration zero every enabled channel together whatever it says "
                "(ZPA p.47)."
            ),
        )
    for c in _CHANNELS_5:
        yield _setting(
            f"calibration.ch{c}.range_mode",
            "Calibration scope",
            0x1E + c - 1,
            write=SafetyTier.DANGEROUS,
            ref="TN5A1190a p.29",
            doc=f"Ch{c} calibration, manual or automatic, adjusts the current range or both.",
            channel=_ch(c),
            enum=CalibrationRangeMode,
            notes="'Both' calibrates range 1 and range 2 together (ZPA p.45).",
        )


def _alarm_settings() -> Iterator[RegisterSpec]:
    common = {"requires": Capability.ALARMS}
    for n in _ALARMS_5:
        for r in _RANGES:
            for k, kind in enumerate(_LIMIT_KINDS):
                yield _setting(
                    f"alarm{n}.range{r}.{kind}",
                    "Alarms",
                    0x23 + 4 * (n - 1) + 2 * (r - 1) + k,
                    ref="TN5A1190a p.29",
                    doc=f"Alarm {n} range {r} {kind} limit.",
                    minimum=0,
                    maximum=9999,
                    scaling=_BY_ALARM,
                    alarm=n,
                    rng=r,
                    notes=_BY_ALARM_TARGET_NOTE
                    + " The manual labels these 'Ch1-Ch5'; they are per alarm (design §2.6). "
                    + _ALARM_LIMIT_NOTE,
                    **common,
                )
    for n in _ALARMS_5:
        yield _setting(
            f"alarm{n}.mode",
            "Alarms",
            0x37 + n - 1,
            ref="TN5A1190a p.30",
            doc=f"Alarm {n} mode.",
            alarm=n,
            enum=AlarmMode,
            **common,
        )
    for n in _ALARMS_5:
        yield _setting(
            f"alarm{n}.enabled",
            "Alarms",
            0x3C + n - 1,
            ref="TN5A1190a p.30",
            doc=f"Alarm {n} on/off.",
            alarm=n,
            dtype=DataType.BOOL,
            notes="Switch the alarm off before changing its settings (ZPA p.48).",
            **common,
        )
    yield _setting(
        "alarm.hysteresis",
        "Alarms",
        0x41,
        ref="TN5A1190a p.30",
        doc="Alarm hysteresis, common to every alarm.",
        minimum=0,
        maximum=20,
        unit="%FS",
        **common,
    )
    for n in range(1, 7):
        yield _setting(
            f"alarm{n}.target_channel",
            "Alarms",
            0x78 + n - 1,
            ref="TN5A1190a p.31",
            doc=f"Alarm {n} target channel, raw.",
            evidence=Evidence.CONTESTED,
            alarm=n,
            notes=(
                "The manual gives 0-6 without saying what the values mean. The bench unit "
                "reads 0-4 for alarms 1-5 (channel - 1?) and 12 for alarm 6."
            ),
            **common,
        )
    for r in _RANGES:
        for k, kind in enumerate(_LIMIT_KINDS):
            yield _setting(
                f"alarm6.range{r}.{kind}",
                "Alarms",
                0x7E + 2 * (r - 1) + k,
                ref="TN5A1190a p.31",
                doc=f"Alarm 6 range {r} {kind} limit.",
                minimum=0,
                maximum=9999,
                scaling=_BY_ALARM,
                alarm=6,
                rng=r,
                notes=_BY_ALARM_TARGET_NOTE + " " + _ALARM_LIMIT_NOTE,
                **common,
            )
    yield _setting(
        "alarm6.mode",
        "Alarms",
        0x82,
        ref="TN5A1190a p.32",
        doc="Alarm 6 mode.",
        alarm=6,
        enum=AlarmMode,
        **common,
    )
    yield _setting(
        "alarm6.enabled",
        "Alarms",
        0x83,
        ref="TN5A1190a p.32",
        doc="Alarm 6 on/off.",
        alarm=6,
        dtype=DataType.BOOL,
        notes="Alarm 6 is an option the ZPA manual never mentions (TN5A1190a p.31).",
        **common,
    )


def _schedule(
    prefix: str,
    group: str,
    base: int,
    *,
    requires: Capability,
    ref: str,
) -> Iterator[RegisterSpec]:
    """Start day, hour and minute of a schedule, at ``base``..``base + 2``."""
    yield _setting(
        f"{prefix}.start_day",
        group,
        base,
        requires=requires,
        ref=ref,
        doc="Start day of week.",
        enum=DayOfWeek,
    )
    for offset, part in ((1, "hour"), (2, "minute")):
        yield _setting(
            f"{prefix}.start_{part}",
            group,
            base + offset,
            requires=requires,
            ref=ref,
            doc=f"Start {part}, raw.",
            evidence=Evidence.CONTESTED,
            notes=_CONTESTED_BCD,
        )


def _automatic_functions() -> Iterator[RegisterSpec]:
    auto_cal = {"requires": Capability.AUTO_CALIBRATION}
    yield from _schedule(
        "auto_calibration",
        "Auto calibration",
        0x42,
        ref="TN5A1190a p.30",
        requires=Capability.AUTO_CALIBRATION,
    )
    yield _setting(
        "auto_calibration.cycle",
        "Auto calibration",
        0x45,
        ref="TN5A1190a p.30",
        doc="Auto calibration cycle, in the cycle unit.",
        notes=_NO_CYCLE_LIMITS,
        **auto_cal,
    )
    yield _setting(
        "auto_calibration.cycle_unit",
        "Auto calibration",
        0x46,
        ref="TN5A1190a p.30",
        doc="Auto calibration cycle unit.",
        enum=ScheduleCycleUnit,
        **auto_cal,
    )
    yield _setting(
        "auto_calibration.enabled",
        "Auto calibration",
        0x47,
        ref="TN5A1190a p.30",
        doc="Auto calibration on/off.",
        dtype=DataType.BOOL,
        notes=(
            "Switch it off before changing the schedule (ZPA p.53). A schedule resumes "
            "after a power failure at its next start time (ZPA p.55)."
        ),
        **auto_cal,
    )
    for c in _CHANNELS_5:
        yield _setting(
            f"auto_calibration.ch{c}.range",
            "Auto calibration",
            0x73 + c - 1,
            ref="TN5A1190a p.31",
            doc=f"Range Ch{c} is calibrated on by auto calibration and auto zero calibration.",
            channel=_ch(c),
            enum=RangeIndex,
            notes=(
                "The channel switches to this range for the calibration and back afterwards, "
                "so the current range can change during one (ZPA p.46)."
            ),
            **auto_cal,
        )
    for k in range(1, 8):
        yield _setting(
            f"auto_calibration.flow_time{k}",
            "Auto calibration",
            0x84 + k - 1,
            ref="TN5A1190a p.32",
            doc=f"Auto calibration gas flow time {k}.",
            minimum=60,
            maximum=900,
            unit="s",
            notes=(
                "Which gas each time belongs to is not stated. The ZPA flow-time screen "
                "suggests 1 zero, 2-6 Ch1-Ch5 span, 7 the hold extension after calibration "
                "(inferred, ZPA p.31, p.54-55). Each should be at least five times the "
                "channel's response time (ZPA p.54)."
            ),
            **auto_cal,
        )

    auto_zero = {"requires": Capability.AUTO_ZERO}
    yield from _schedule(
        "auto_zero",
        "Auto zero calibration",
        0x62,
        ref="TN5A1190a p.31",
        requires=Capability.AUTO_ZERO,
    )
    yield _setting(
        "auto_zero.cycle",
        "Auto zero calibration",
        0x65,
        ref="TN5A1190a p.31",
        doc="Auto zero calibration cycle, in the cycle unit.",
        notes=_NO_CYCLE_LIMITS.replace("p.55", "p.60"),
        **auto_zero,
    )
    yield _setting(
        "auto_zero.cycle_unit",
        "Auto zero calibration",
        0x66,
        ref="TN5A1190a p.31",
        doc="Auto zero calibration cycle unit.",
        enum=ScheduleCycleUnit,
        **auto_zero,
    )
    yield _setting(
        "auto_zero.enabled",
        "Auto zero calibration",
        0x67,
        ref="TN5A1190a p.31",
        doc="Auto zero calibration on/off.",
        dtype=DataType.BOOL,
        notes=(
            "Switch it off before changing the schedule (ZPA p.59). Where it falls due "
            "with an auto calibration, the auto calibration runs instead (ZPA p.60)."
        ),
        **auto_zero,
    )
    yield _setting(
        "auto_zero.flow_time",
        "Auto zero calibration",
        0x68,
        ref="TN5A1190a p.31",
        doc="Auto zero calibration gas flow time.",
        minimum=60,
        maximum=900,
        unit="s",
        notes="The gas replacement time after the calibration is the same (ZPA p.60).",
        **auto_zero,
    )

    blowback = {"requires": Capability.BLOWBACK}
    yield from _schedule(
        "blowback",
        "Blowback",
        0x91,
        ref="TN5A1190a p.32",
        requires=Capability.BLOWBACK,
    )
    yield _setting(
        "blowback.cycle",
        "Blowback",
        0x94,
        ref="TN5A1190a p.32",
        doc="Blowback cycle, in the cycle unit.",
        minimum=1,
        maximum=99,
        notes="1-99 in hours, 1-7 in days.",
        **blowback,
    )
    yield _setting(
        "blowback.cycle_unit",
        "Blowback",
        0x95,
        ref="TN5A1190a p.32",
        doc="Blowback cycle unit.",
        enum=ScheduleCycleUnit,
        **blowback,
    )
    yield _setting(
        "blowback.duration",
        "Blowback",
        0x96,
        ref="TN5A1190a p.32",
        doc="Blowback time.",
        minimum=1,
        maximum=900,
        unit="s",
        **blowback,
    )
    yield _setting(
        "blowback.enabled",
        "Blowback",
        0x97,
        ref="TN5A1190a p.32",
        doc="Blowback on/off.",
        dtype=DataType.BOOL,
        notes=(
            "Blowback appears in neither the ZPA manual nor its code table, nor in the "
            "service manual's ZPA parts list (TN5A1191b p.10)."
        ),
        **blowback,
    )
    yield _setting(
        "blowback.displacement_time",
        "Blowback",
        0x98,
        ref="TN5A1190a p.32",
        doc="Gas displacement time after blowback.",
        minimum=60,
        maximum=900,
        unit="s",
        **blowback,
    )


def _measurement_settings() -> Iterator[RegisterSpec]:
    persistent = SafetyTier.PERSISTENT
    yield _setting(
        "key_lock",
        "Key lock",
        0x49,
        ref="TN5A1190a p.30",
        doc="Front-panel key lock on/off.",
        dtype=DataType.BOOL,
        notes=(
            "Locks every panel key but the key lock itself, including the forced stop of a "
            "running auto calibration (ZPA p.55, p.64). It does not block Modbus writes: "
            "the bench unit applied a setting written while it was on (protocol findings "
            "§13.3)."
        ),
    )
    for k in range(1, 5):
        yield _setting(
            f"response_time.ndir{k}",
            "Response time",
            0x4B + 2 * (k - 1),
            write=persistent,
            ref="TN5A1190a p.30",
            doc=f"Response time of NDIR component {k}.",
            minimum=1,
            maximum=60,
            unit="s",
            notes=(
                "The manual's 'Ch1-Ch4' slots are NDIR components; O2 has its own slot "
                "(40084) whatever its channel. The MODBUS manual gives 0-60 s, the ZPA "
                "manual 1-60 s (ZPA p.65); a write is kept to 1-60 s."
            ),
        )
    yield _setting(
        "response_time.o2",
        "Response time",
        0x53,
        write=persistent,
        ref="TN5A1190a p.30",
        doc="Response time of the O2 measurement, whatever its channel.",
        minimum=1,
        maximum=60,
        unit="s",
        notes=(
            "The MODBUS manual gives 0-60 s, the ZPA manual 1-60 s (ZPA p.65); a write is "
            "kept to 1-60 s."
        ),
    )
    averaging = {"requires": Capability.AVERAGING}
    for k in range(1, 5):
        yield _setting(
            f"moving_average{k}.period",
            "Moving average",
            0x54 + k - 1,
            ref="TN5A1190a p.30",
            doc=f"Moving-average period {k}, in its unit.",
            minimum=0,
            maximum=59,
            notes=(
                "Which output each of the four 'orders' averages is not stated. The ZPA "
                "manual gives 1-59 min or 1-4 h (ZPA p.65, p.68). Changing a period "
                "restarts that average (ZPA p.68)."
            ),
            **averaging,
        )
    for k in range(1, 5):
        yield _setting(
            f"moving_average{k}.unit",
            "Moving average",
            0x58 + k - 1,
            ref="TN5A1190a p.30",
            doc=f"Moving-average period {k} unit.",
            enum=PeriodUnit,
            **averaging,
        )
    yield _setting(
        "output_hold.enabled",
        "Output hold",
        0x5C,
        write=persistent,
        ref="TN5A1190a p.31",
        doc="Hold the outputs during calibration, on/off.",
        dtype=DataType.BOOL,
        notes=(
            "Holds the analog outputs and the Modbus concentration registers during a "
            "manual or automatic calibration and its gas-replacement time; the display is "
            "never held (ZPA p.65, p.67)."
        ),
    )
    correction = {"requires": Capability.O2_CORRECTION}
    yield _setting(
        "o2_correction.reference",
        "O2 correction",
        0x5D,
        ref="TN5A1190a p.31",
        doc="O2 correction reference value.",
        minimum=1,
        maximum=19,
        unit="vol%",
        notes=(
            "The ZPA manual gives 0-19 %, set in the password-protected maintenance mode "
            "(ZPA p.73)."
        ),
        **correction,
    )
    yield _setting(
        "o2_correction.limit",
        "O2 correction",
        0x9D,
        ref="TN5A1190a p.32",
        doc="O2 limit for correction.",
        minimum=1,
        maximum=20,
        unit="vol%",
        notes="Set in the password-protected maintenance mode (ZPA p.73).",
        **correction,
    )
    peak = "The CO peak alarm is an option (ZPA p.51)."
    yield _setting(
        "peak_alarm.enabled",
        "Peak alarm",
        0x5E,
        ref="TN5A1190a p.31",
        doc="Peak alarm on/off.",
        dtype=DataType.BOOL,
        notes=peak + " Switching it on restarts the count from 0 (ZPA p.52).",
    )
    yield _setting(
        "peak_alarm.concentration",
        "Peak alarm",
        0x5F,
        ref="TN5A1190a p.31",
        doc="Peak alarm concentration.",
        minimum=100,
        maximum=1000,
        unit="ppm",
        notes=peak + " The ZPA manual gives 10-1000 ppm in 5 ppm steps (ZPA p.52).",
    )
    yield _setting(
        "peak_alarm.count",
        "Peak alarm",
        0x60,
        ref="TN5A1190a p.31",
        doc="Peak alarm count.",
        minimum=1,
        maximum=99,
        notes=peak,
    )
    yield _setting(
        "peak_alarm.hysteresis",
        "Peak alarm",
        0x61,
        ref="TN5A1190a p.31",
        doc="Peak alarm hysteresis.",
        minimum=0,
        maximum=20,
        unit="%FS",
        notes=peak,
    )
    for c in _CHANNELS_5:
        yield _setting(
            f"range.ch{c}.selected",
            "Ranges",
            0x69 + c - 1,
            write=persistent,
            ref="TN5A1190a p.31",
            doc=f"Ch{c} selected range, used while the range method is manual.",
            channel=_ch(c),
            enum=RangeIndex,
            notes=(
                "The MODBUS manual says it is ignored while remote range is on; the ZPA "
                "manual, while the method is remote or auto (ZPA p.40). A write needs the "
                "method to be manual. The current range follows some tens of milliseconds "
                "after the write reads back (protocol findings §13.2); fujilib waits for it."
            ),
        )
    for c in _CHANNELS_5:
        yield _setting(
            f"range.ch{c}.method",
            "Ranges",
            0x6E + c - 1,
            write=persistent,
            ref="TN5A1190a p.31",
            doc=f"Ch{c} range-change method.",
            channel=_ch(c),
            enum=RangeMethod,
            write_values=frozenset({int(RangeMethod.MANUAL), int(RangeMethod.AUTO)}),
            notes=(
                "Remote range follows an input contact of the DIO option (ZPA p.29), so it "
                "is not written."
            ),
        )
    yield _setting(
        "hold.mode",
        "Hold",
        0x8B,
        write=persistent,
        ref="TN5A1190a p.32",
        doc="What the outputs hold during calibration: the last value or the set value.",
        enum=HoldMode,
        notes="On the panel it can be chosen only while output hold is on (ZPA p.66).",
    )
    for c in _CHANNELS_5:
        yield _setting(
            f"hold.ch{c}.value",
            "Hold",
            0x8C + c - 1,
            write=persistent,
            ref="TN5A1190a p.32",
            doc=f"Ch{c} hold set value.",
            minimum=0,
            maximum=100,
            unit="%FS",
            channel=_ch(c),
            notes="In percent of the full scale of whichever range is in use (ZPA p.67).",
        )
    point = {"requires": Capability.MEASUREMENT_POINT}
    yield _setting(
        "measurement_point.cycle",
        "Measurement point",
        0x99,
        ref="TN5A1190a p.32",
        doc="Measurement-point change cycle, in its unit.",
        minimum=1,
        maximum=99,
        notes="1-60 in minutes, 1-99 in hours. Not in the ZPA manual.",
        **point,
    )
    yield _setting(
        "measurement_point.cycle_unit",
        "Measurement point",
        0x9A,
        ref="TN5A1190a p.32",
        doc="Measurement-point change cycle unit.",
        enum=PeriodUnit,
        **point,
    )
    yield _setting(
        "measurement_point.displacement_time",
        "Measurement point",
        0x9B,
        ref="TN5A1190a p.32",
        doc="Measurement-point displacement time.",
        minimum=60,
        maximum=900,
        unit="s",
        **point,
    )
    yield _setting(
        "measurement_point.mode",
        "Measurement point",
        0x9C,
        ref="TN5A1190a p.32",
        doc="Measurement-point setting.",
        enum=MeasurementPoint,
        **point,
    )


def _model_specific() -> Iterator[RegisterSpec]:
    reference = {"requires": Capability.REFERENCE_GAS}
    yield _setting(
        "reference_gas.switching_time",
        "Reference gas (ZPB/ZPG)",
        0x9E,
        ref="TN5A1190a p.32",
        doc="Reference-gas switching time.",
        minimum=1,
        maximum=30,
        unit="s",
        **reference,
    )
    yield _setting(
        "reference_gas.measuring_time",
        "Reference gas (ZPB/ZPG)",
        0x9F,
        ref="TN5A1190a p.32",
        doc="Reference-gas measuring time.",
        minimum=1,
        maximum=60,
        unit="s",
        **reference,
    )
    for c in range(1, 5):
        yield _setting(
            f"reference_gas.ch{c}.average",
            "Reference gas (ZPB/ZPG)",
            0xA0 + c - 1,
            ref="TN5A1190a p.32",
            doc=f"Ch{c} average period, in cycles.",
            minimum=0,
            maximum=9,
            channel=_ch(c),
            **reference,
        )
    for k in range(1, 5):
        yield _setting(
            f"interference.coefficient{k}",
            "Interference compensation",
            0xA4 + 2 * (k - 1),
            count=2,
            dtype=DataType.UINT32_LH,
            evidence=Evidence.INFERRED,
            ref="TN5A1190a p.32",
            doc=f"Interference compensation coefficient {k} (interpretation inferred).",
            notes=(
                "The manual lists 00A4h and 00A5h as two words with no limits and does not "
                "list 00A6h-00ABh. The bench unit reads four long words of 1,000,000."
            ),
        )


# --- Input registers --------------------------------------------------------


def _readings() -> Iterator[RegisterSpec]:
    for n in range(1, 13):
        base = 3 * (n - 1)
        yield _input(
            f"reading.ch{n}.value",
            "Readings",
            base,
            dtype=DataType.INT16,
            ref="TN5A1190a p.34",
            doc=f"Ch{n} concentration, without its decimal point.",
            minimum=-9999,
            maximum=9999,
            scaling=_INLINE,
            channel=_ch(n),
            notes="Over-range is not flagged: the computed value is still sent.",
        )
        yield _input(
            f"reading.ch{n}.decimals",
            "Readings",
            base + 1,
            ref="TN5A1190a p.34",
            doc=f"Ch{n} decimal-point position.",
            minimum=0,
            maximum=MAX_DECIMALS,
            channel=_ch(n),
        )
        yield _input(
            f"reading.ch{n}.unit",
            "Readings",
            base + 2,
            ref="TN5A1190a p.34",
            doc=f"Ch{n} unit code (0 vol%, 1 ppm, 2 mg/m3, 3 g/m3).",
            minimum=0,
            maximum=3,
            channel=_ch(n),
        )


def _status() -> Iterator[RegisterSpec]:
    ref = "TN5A1190a p.35"
    yield _input(
        "peak_alarm.count_per_hour",
        "Status",
        0x24,
        ref=ref,
        doc="Peak count per hour.",
        minimum=0,
        maximum=100,
    )
    for c in _CHANNELS_5:
        yield _input(
            f"range.ch{c}.current",
            "Status",
            0x25 + c - 1,
            ref=ref,
            doc=f"Ch{c} current range.",
            channel=_ch(c),
            enum=RangeIndex,
        )
    for n in _ALARMS_5:
        yield _input(
            f"alarm{n}.state",
            "Status",
            0x2A + n - 1,
            ref=ref,
            doc=f"Alarm {n} state.",
            alarm=n,
            notes="The manual labels these 'Ch1-Ch5'; they are per alarm (design §2.6).",
            enum=AlarmState,
            requires=Capability.ALARMS,
        )
    yield _input(
        "peak_alarm.active", "Status", 0x2F, ref=ref, doc="Peak count alarm.", dtype=DataType.BOOL
    )
    yield _input(
        "status.auto_calibration_running",
        "Status",
        0x30,
        ref=ref,
        doc="Auto calibration or auto zero calibration in progress.",
        dtype=DataType.BOOL,
    )
    for c in _CHANNELS_5:
        yield _input(
            f"status.ch{c}.zero_calibrating",
            "Status",
            0x31 + c - 1,
            ref=ref,
            doc=f"Ch{c} zero calibration in progress.",
            channel=_ch(c),
            dtype=DataType.BOOL,
        )
    for c in _CHANNELS_5:
        yield _input(
            f"status.ch{c}.span_calibrating",
            "Status",
            0x36 + c - 1,
            ref=ref,
            doc=f"Ch{c} span calibration in progress.",
            channel=_ch(c),
            dtype=DataType.BOOL,
        )
    yield _input(
        "status.instrument_error",
        "Status",
        0x3B,
        ref=ref,
        doc="Instrument error.",
        dtype=DataType.BOOL,
    )
    yield _input(
        "status.calibration_error",
        "Status",
        0x3C,
        ref=ref,
        doc="Calibration error.",
        dtype=DataType.BOOL,
    )
    for address, e in ((0x83, 1), (0x84, 2), (0x85, 3), (0x86, 10)):
        yield _input(
            f"error.e{e}.active",
            "Errors",
            address,
            ref="TN5A1190a p.36",
            doc=f"Error {e} currently active.",
            dtype=DataType.BOOL,
        )
    for c in _CHANNELS_5:
        for e in range(4, 10):
            yield _input(
                f"error.ch{c}.e{e}.active",
                "Errors",
                0x87 + 6 * (c - 1) + (e - 4),
                ref="TN5A1190a p.36",
                doc=f"Ch{c} error {e} currently active.",
                channel=_ch(c),
                dtype=DataType.BOOL,
            )
    for c in _CHANNELS_5:
        for k, (field_name, what) in enumerate(
            (
                ("auto_zero_running", "auto zero calibration"),
                ("auto_span_running", "auto span calibration"),
                ("hold", "hold"),
            )
        ):
            yield _input(
                f"status.ch{c}.{field_name}",
                "Status",
                0xA5 + 3 * (c - 1) + k,
                ref="TN5A1190a p.37",
                doc=f"Ch{c} {what} in progress.",
                channel=_ch(c),
                dtype=DataType.BOOL,
            )
    yield _input(
        "display.screen",
        "Display",
        0xB4,
        ref="TN5A1190a p.37",
        doc="Front-panel screen.",
        enum=DisplayScreen,
    )
    yield _input(
        "display.calibration_step",
        "Display",
        0xB5,
        ref="TN5A1190a p.37",
        doc="Manual-calibration step shown on the panel.",
        enum=ManualCalibrationStep,
    )
    yield _input(
        "display.top_channel",
        "Display",
        0xB6,
        ref="TN5A1190a p.37",
        doc="Top channel on the panel, channel number - 1.",
        minimum=0,
        maximum=11,
    )
    yield _input(
        "display.cursor_channel",
        "Display",
        0xBC,
        ref="TN5A1190a p.37",
        doc="Channel under the manual-calibration cursor, channel number - 1.",
        minimum=0,
        maximum=11,
    )
    yield _input(
        "alarm6.state",
        "Status",
        0xBE,
        ref="TN5A1190a p.37",
        doc="Alarm 6 state.",
        alarm=6,
        notes="The manual gives no domain; taken to be that of alarms 1-5.",
        enum=AlarmState,
        requires=Capability.ALARMS,
    )


def _fixed_settings() -> Iterator[RegisterSpec]:
    ref = "TN5A1190a p.38"
    for c in _CHANNELS_5:
        yield _input(
            f"range.ch{c}.count",
            "Ranges",
            0x425 + c - 1,
            ref=ref,
            doc=f"Ch{c} number of ranges. Unused channels also report ranges.",
            minimum=1,
            maximum=2,
            channel=_ch(c),
        )
    for c in _CHANNELS_5:
        for r in _RANGES:
            offset = 2 * (c - 1) + (r - 1)
            yield _input(
                f"range.ch{c}.range{r}.unit",
                "Ranges",
                0x42A + offset,
                ref=ref,
                doc=f"Ch{c} range {r} unit code.",
                minimum=0,
                maximum=3,
                channel=_ch(c),
                rng=r,
                notes="The manual's list omits '1: ppm'; the other unit tables have it.",
            )
    for c in _CHANNELS_5:
        for r in _RANGES:
            offset = 2 * (c - 1) + (r - 1)
            yield _input(
                f"range.ch{c}.range{r}.full_scale",
                "Ranges",
                0x434 + offset,
                ref=ref,
                doc=f"Ch{c} range {r} full-scale value.",
                minimum=1,
                maximum=9999,
                scaling=_BY_RANGE,
                channel=_ch(c),
                rng=r,
            )
    for c in _CHANNELS_5:
        for r in _RANGES:
            offset = 2 * (c - 1) + (r - 1)
            yield _input(
                f"range.ch{c}.range{r}.decimals",
                "Ranges",
                0x43E + offset,
                ref=ref,
                doc=f"Ch{c} range {r} decimal-point position.",
                minimum=0,
                maximum=MAX_DECIMALS,
                channel=_ch(c),
                rng=r,
            )
    yield _input(
        "identity.type_code",
        "Identity",
        0x448,
        count=26,
        dtype=DataType.CHAR,
        ref="TN5A1190a p.39",
        doc="Type code digits 1-26, one character per register.",
    )
    yield _input(
        "identity.serial_number",
        "Identity",
        0x462,
        count=8,
        dtype=DataType.CHAR,
        ref="TN5A1190a p.39",
        doc="The manual's 'board' code, digits 1-8.",
        notes="On the bench unit it is the serial number (protocol findings §2).",
    )
    yield _input(
        "identity.type_code_ext",
        "Identity",
        0x47A,
        count=3,
        dtype=DataType.CHAR,
        ref="TN5A1190a p.39",
        doc="Type code digits 27-29.",
        requires=Capability.TYPE_CODE_EXT,
    )


#: The service manual's A/D table, in table order (TN5A1191b p.32).
_ADC_NAMES: Final = (
    *(f"input{k}" for k in range(1, 6)),
    *(f"temperature{k}" for k in range(1, 6)),
    *(f"resistance{k}_1" for k in range(1, 5)),
    "pressure",
    "reference_voltage",
    "ground",
    *(f"resistance{k}_2" for k in range(1, 5)),
)


def _service() -> Iterator[RegisterSpec]:
    ref = "protocol findings §4.1"
    for k, part in enumerate(("year", "month", "day", "weekday", "hour", "minute", "second")):
        yield _input(
            f"clock.{part}",
            "Clock",
            0x3E8 + k,
            dtype=DataType.BCD,
            evidence=Evidence.OBSERVED,
            ref=ref,
            doc=f"Real-time clock {part}, BCD" + (", two digits." if part == "year" else "."),
            requires=Capability.CLOCK,
        )
    for k, name in enumerate(_ADC_NAMES):
        yield _input(
            f"adc.{name}",
            "A/D values",
            0x3EF + 2 * k,
            count=2,
            dtype=DataType.UINT32_LH,
            evidence=Evidence.INFERRED,
            ref="TN5A1191b p.32",
            doc=f"A/D value No. {k}, a raw service count.",
            requires=Capability.ADC_VALUES,
            notes="Names follow the service manual's table order; the block is undocumented.",
        )


# --- Logs ---------------------------------------------------------------------

ERROR_LOG: Final = LogSpec(
    name="error_log",
    group="Error log",
    table=RegisterTable.INPUT,
    base=0x3D,
    record_words=5,
    records=14,
    fields=(
        LogField("code", 0, DataType.INT16, "Error number - 1; -1 marks an empty entry."),
        LogField("day", 1, DataType.UINT16, "Day of month, 1-31."),
        LogField("hour", 2, DataType.UINT16, "Hour, 0-23."),
        LogField("minute", 3, DataType.UINT16, "Minute, 0-59."),
        LogField("target", 4, DataType.UINT16, "Channel number - 1; 0 for errors 3 and 10."),
    ),
    evidence=Evidence.DOCUMENTED,
    manual_ref="TN5A1190a p.35-36, p.46",
    doc="The 14 newest errors, newest first. No year and no month.",
)

CALIBRATION_LOG: Final = LogSpec(
    name="calibration_log",
    group="Calibration log",
    table=RegisterTable.INPUT,
    base=0x1000,
    record_words=9,
    records=40,
    channels=5,
    channel_stride=360,
    fields=(
        LogField("channel", 0, DataType.INT16, "Channel, -1 to 5; -1 marks an empty record."),
        LogField(
            "kind",
            1,
            DataType.ENUM,
            "Range and kind: 0 Z1, 1 S1, 2 Z2, 3 S2.",
            enum=CalibrationKind,
        ),
        LogField(
            "detector_count", 2, DataType.UINT32_LH, "Detector count at calibration.", count=2
        ),
        LogField(
            "deviation",
            4,
            DataType.INT16,
            "Calibration deviation, %FS.",
            scaling=Scaling(ScalingKind.FIXED, 1),
        ),
        LogField("month", 5, DataType.UINT16, "Month, 1-12."),
        LogField("day", 6, DataType.UINT16, "Day of month, 1-31."),
        LogField("hour", 7, DataType.UINT16, "Hour, 0-23."),
        LogField("minute", 8, DataType.UINT16, "Minute, 0-59."),
    ),
    evidence=Evidence.DOCUMENTED,
    manual_ref="TN5A1190a p.40-45",
    doc="Per channel, the 40 newest calibrations, newest first. No year.",
    requires=Capability.CALIBRATION_LOG,
    notes=(
        "Firmware 2.24 or later, so never seen on the bench unit. The deviation's x10 "
        "scaling is implied by its one decimal, not stated. The panel's own log keeps "
        "10 records per component (ZPA p.71)."
    ),
)


# --- Validation and the registry ----------------------------------------------


def _fail(msg: str, table: RegisterTable, address: int) -> NoReturn:
    raise FujiConfigurationError(
        msg, context=ErrorContext(register_address=address, extra={"table": table.value})
    )


def _check_spec(spec: RegisterSpec, regions: RegionMap) -> None:  # noqa: PLR0912
    def bad(msg: str) -> NoReturn:
        _fail(f"{spec.name}: {msg}", spec.table, spec.address)

    width = spec.dtype.fixed_width
    if spec.count < 1 or (width is not None and spec.count != width):
        bad(f"count {spec.count} does not fit {spec.dtype.value}")
    if spec.read_functions != frozenset({spec.table.read_function}):
        bad("read functions do not match its table")
    for fc in sorted(spec.read_functions | spec.write_functions):
        region = regions.region_for(fc, spec.address, spec.count)
        if region is None:
            bad(f"not inside a FC{fc:02X} region")
        if region.requires and not spec.requires & region.requires:
            bad(f"its region requires {region.requires}")
    if spec.writable:
        if spec.table is not RegisterTable.HOLDING or not spec.write_functions:
            bad("writable but not a holding register with write functions")
        if spec.evidence is not Evidence.DOCUMENTED:
            bad("only documented registers may be writable")
        if spec.safety <= SafetyTier.STATEFUL:
            bad("a writable setting must be PERSISTENT or DANGEROUS")
        for fc in sorted(spec.write_functions):
            if not envelope_allows(fc, spec.address, spec.count):
                bad(f"FC{fc:02X} is outside the write envelope")
        if spec.count != 1 or FC_WRITE_SINGLE not in spec.write_functions:
            bad("a writable setting must be one word that FC06 can write")
        if (spec.name, spec.address) not in REVIEWED_SETTINGS:
            bad("only the reviewed settings may be writable (design §5.4)")
    elif spec.write_functions or spec.safety is not SafetyTier.READ_ONLY:
        bad("read-only but has write functions or a write tier")
    elif spec.write_values is not None or spec.write_percent_fs is not None:
        bad("read-only but has write limits")
    if not spec.manual_ref:
        bad("no manual or findings reference")
    if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
        bad("minimum exceeds maximum")
    _check_write_limits(spec, bad)
    if (spec.dtype is DataType.ENUM) != (spec.enum is not None):
        bad("an ENUM type and an enum class must go together")
    if spec.enum is not None and (spec.minimum, spec.maximum) != (min(spec.enum), max(spec.enum)):
        bad(f"limits do not match {spec.enum.__name__}")
    if spec.dtype is DataType.BOOL and (spec.minimum, spec.maximum) != (0, 1):
        bad("a flag's limits must be 0..1")
    _check_scaling(spec, bad)


def _check_write_limits(spec: RegisterSpec, bad: Callable[[str], NoReturn]) -> None:
    if spec.write_values is not None:
        low = spec.minimum if spec.minimum is not None else 0
        high = spec.maximum if spec.maximum is not None else 0xFFFF
        if not spec.write_values or not all(low <= v <= high for v in spec.write_values):
            bad("write values must be some of the values its limits allow")
    if spec.write_percent_fs is not None:
        low, high = spec.write_percent_fs
        if spec.scaling.kind is not ScalingKind.BY_RANGE:
            bad("percent-of-full-scale limits need range scaling")
        if not 0 <= low <= high:
            bad("percent-of-full-scale limits must be 0 <= low <= high")


def _check_scaling(spec: RegisterSpec, bad: Callable[[str], NoReturn]) -> None:
    kind = spec.scaling.kind
    if (kind is ScalingKind.FIXED) != (spec.scaling.decimals is not None):
        bad("only a fixed scaling carries decimals")
    if kind is ScalingKind.BY_RANGE and (spec.channel is None or spec.range is None):
        bad("range scaling needs a channel and a range")
    if kind is ScalingKind.BY_ALARM_TARGET and (spec.alarm is None or spec.range is None):
        bad("alarm-target scaling needs an alarm and a range")
    if kind is ScalingKind.INLINE and spec.channel is None:
        bad("inline scaling needs a channel")


def validate_map(
    specs: Sequence[RegisterSpec],
    logs: Sequence[LogSpec],
    regions: RegionMap,
) -> None:
    """Validate a register map; see the module docstring for the rules.

    Takes the map as arguments so tests can feed it deliberately broken tables.

    Raises:
        FujiConfigurationError: the first rule the map breaks.
    """
    names: set[str] = set()
    spans: list[tuple[RegisterTable, int, int, str]] = []
    for spec in specs:
        if spec.name in names:
            _fail(f"duplicate register name {spec.name!r}", spec.table, spec.address)
        names.add(spec.name)
        _check_spec(spec, regions)
        spans.append((spec.table, spec.address, spec.last_address, spec.name))
    for log in logs:
        if log.name in names:
            _fail(f"duplicate name {log.name!r}", log.table, log.base)
        names.add(log.name)
        size = log.last_address - log.base + 1
        if regions.region_for(log.table.read_function, log.base, size) is None:
            _fail(f"{log.name}: not inside one region", log.table, log.base)
        for fld in log.fields:
            if fld.offset + fld.count > log.record_words:
                _fail(f"{log.name}.{fld.name}: outside the record", log.table, log.base)
        spans.append((log.table, log.base, log.last_address, log.name))
    spans.sort()
    for before, after in pairwise(spans):
        if before[0] is after[0] and after[1] <= before[2]:
            _fail(f"{before[3]} and {after[3]} overlap", after[0], after[1])


@dataclass(frozen=True, slots=True, eq=False)
class RegisterRegistry:
    """The validated register map, indexed by name and by address.

    Construction validates the map (:func:`validate_map`). The indexes are
    read-only.
    """

    specs: tuple[RegisterSpec, ...]
    logs: tuple[LogSpec, ...]
    regions: RegionMap
    _by_name: Mapping[str, RegisterSpec] = field(init=False, repr=False)
    _logs_by_name: Mapping[str, LogSpec] = field(init=False, repr=False)
    _by_address: Mapping[tuple[RegisterTable, int], RegisterSpec] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_map(self.specs, self.logs, self.regions)
        ordered = tuple(
            sorted(self.specs, key=lambda s: (s.table != RegisterTable.HOLDING, s.address))
        )
        object.__setattr__(self, "specs", ordered)
        object.__setattr__(self, "_by_name", MappingProxyType({s.name: s for s in ordered}))
        object.__setattr__(
            self, "_logs_by_name", MappingProxyType({lg.name: lg for lg in self.logs})
        )
        by_address = {
            (s.table, a): s for s in ordered for a in range(s.address, s.last_address + 1)
        }
        object.__setattr__(self, "_by_address", MappingProxyType(by_address))

    def __iter__(self) -> Iterator[RegisterSpec]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def has(self, name: str) -> bool:
        """Whether ``name`` is a register of the map. Never raises."""
        return name in self._by_name

    def resolve(self, name: str) -> RegisterSpec:
        """Return the canonical spec named ``name``.

        Raises:
            FujiValidationError: no register has that name.
        """
        try:
            return self._by_name[name]
        except KeyError:
            close = difflib.get_close_matches(name, self._by_name, n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            msg = f"unknown register {name!r}{hint}"
            raise FujiValidationError(msg, context=ErrorContext(extra={"name": name})) from None

    def log(self, name: str) -> LogSpec:
        """Return the log named ``name``.

        Raises:
            FujiValidationError: no log has that name.
        """
        try:
            return self._logs_by_name[name]
        except KeyError:
            msg = f"unknown log {name!r}"
            raise FujiValidationError(msg, context=ErrorContext(extra={"name": name})) from None

    def at(self, table: RegisterTable, address: int) -> RegisterSpec | None:
        """The register occupying ``address`` of ``table``, if any."""
        return self._by_address.get((table, address))

    def select(self, prefix: str) -> tuple[RegisterSpec, ...]:
        """Every register whose name is ``prefix`` or starts with ``prefix.``, in map order."""
        return tuple(s for s in self.specs if s.name == prefix or s.name.startswith(f"{prefix}."))

    def in_table(self, table: RegisterTable) -> tuple[RegisterSpec, ...]:
        """Every register of ``table``, in address order."""
        return tuple(s for s in self.specs if s.table is table)

    def groups(self) -> tuple[str, ...]:
        """Register group names, in map order."""
        return tuple(dict.fromkeys(s.group for s in self.specs))


def _build_specs() -> tuple[RegisterSpec, ...]:
    builders: Iterable[Iterable[RegisterSpec]] = (
        _calibration_settings(),
        _alarm_settings(),
        _automatic_functions(),
        _measurement_settings(),
        _model_specific(),
        _readings(),
        _status(),
        _fixed_settings(),
        _service(),
    )
    return tuple(spec for builder in builders for spec in builder)


#: The ZP-series register map, validated at import.
REGISTRY: Final = RegisterRegistry(_build_specs(), (ERROR_LOG, CALIBRATION_LOG), ZP_REGIONS)
