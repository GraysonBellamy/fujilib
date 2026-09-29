"""The analyzer's settings as a document: compared and applied (design §6.3, §7.7).

A settings document is what ``fuji-configure dump`` writes (format
``fujilib-settings/1``): the analyzer's identity, then each holding register
by name with its ``value`` and ``unit`` (and ``raw``, ``access``, ``safety``
and ``evidence``, which are informative). A document written by hand may give
only the settings to change, each as ``{"value": ..., "unit": ...}`` or as a
bare value.

**Comparing** (:func:`diff_settings`) decides, for every setting the document
names, one :class:`ChangeAction`:

- ``unchanged``: the analyzer has that value already;
- ``write``: it differs, the register is writable, and the value passes every
  check that can be made before writing, against the range tables read just
  before;
- ``refused``: an unknown name, an operation, an input register, a read-only
  register whose value differs, a value that does not fit, a unit other than
  the range's, or a range selection whose method would not be manual.

A document from another analyzer (another serial number) is refused as a
whole unless the caller says any analyzer will do.

**Applying** (:meth:`~fujilib.devices.analyzer.Analyzer.apply_settings`)
preflights the whole document first: if anything is refused, nothing is
written. The writes then go in a fixed order, each a setting write of its own
(read back, never retried): output hold before the hold settings when it is
switched on, after them when it is switched off; a range method before its
range; then the response times, the calibration scope and the calibration
gases. The first write that fails stops the rest. The report says which
writes completed, which failed and which were not attempted. Nothing is
rolled back or switched back on.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from fujilib.devices.capability import SafetyTier
from fujilib.devices.decode import range_scaling
from fujilib.devices.encode import encode_prepared, prepare_value
from fujilib.errors import (
    ErrorContext,
    FujiError,
    FujiValidationError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.modbus.codec import as_decimal
from fujilib.registry.enums import RangeIndex, RangeMethod
from fujilib.registry.regions import RegisterTable
from fujilib.registry.registers import ScalingKind
from fujilib.registry.units import coerce_unit
from fujilib.registry.write_policy import OPERATIONS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import RangeInfo
    from fujilib.devices.writes import WriteResult
    from fujilib.registry.registers import RegisterRegistry, RegisterSpec
    from fujilib.registry.units import Unit

__all__ = [
    "SETTINGS_FORMAT",
    "ApplyReport",
    "ApplyStatus",
    "ChangeAction",
    "DesiredSetting",
    "SettingChange",
    "SettingsDiff",
    "SettingsDocument",
    "diff_settings",
]

#: The version of the settings document's layout.
SETTINGS_FORMAT: Final = "fujilib-settings/1"


# --- The document ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DesiredSetting:
    """One setting as a document gives it."""

    name: str
    value: bool | int | float | str | None
    unit: str | None = None
    raw: int | None = None
    """The raw word, compared only when ``value`` is ``None`` (an alarm limit with no scale)."""


@dataclass(frozen=True, slots=True)
class SettingsDocument:
    """A settings document: the analyzer it describes, and its settings by name."""

    settings: Mapping[str, DesiredSetting]
    serial_number: str | None = None
    type_code: str | None = None
    model: str | None = None

    @classmethod
    def from_json(cls, data: object) -> SettingsDocument:
        """Read a document, as parsed from JSON.

        Raises:
            FujiValidationError: it is not a ``fujilib-settings/1`` document.
        """
        if not isinstance(data, dict):
            msg = "a settings document is a JSON object"
            raise FujiValidationError(msg)
        document = cast("dict[str, object]", data)
        if document.get("format") != SETTINGS_FORMAT:
            msg = f"expected format {SETTINGS_FORMAT!r}, got {document.get('format')!r}"
            raise FujiValidationError(msg)
        entries = document.get("settings")
        if not isinstance(entries, dict):
            msg = "a settings document needs a 'settings' object"
            raise FujiValidationError(msg)
        analyzer = document.get("analyzer", {})
        if not isinstance(analyzer, dict):
            msg = "'analyzer' must be an object"
            raise FujiValidationError(msg)
        identity = cast("dict[str, object]", analyzer)
        for key in ("serial_number", "type_code", "model"):
            if identity.get(key) is not None and not isinstance(identity.get(key), str):
                msg = f"'analyzer.{key}' must be text, got {identity.get(key)!r}"
                raise FujiValidationError(msg)
        settings = {
            str(name): _desired(str(name), entry)
            for name, entry in cast("dict[object, object]", entries).items()
        }
        return cls(
            settings=MappingProxyType(settings),
            serial_number=_text(identity.get("serial_number")),
            type_code=_text(identity.get("type_code")),
            model=_text(identity.get("model")),
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _desired(name: str, entry: object) -> DesiredSetting:
    if not isinstance(entry, dict):
        entry = {"value": entry}
    fields = cast("dict[str, object]", entry)
    value = fields.get("value")
    unit = fields.get("unit")
    raw = fields.get("raw")
    if value is not None and not isinstance(value, bool | int | float | str):
        msg = f"{name}: a value is a flag, a number or a name, got {value!r}"
        raise FujiValidationError(msg)
    if unit is not None and not isinstance(unit, str):
        msg = f"{name}: a unit is text, got {unit!r}"
        raise FujiValidationError(msg)
    return DesiredSetting(
        name=name,
        value=value,
        unit=unit,
        raw=raw if isinstance(raw, int) and not isinstance(raw, bool) else None,
    )


# --- Comparing ---------------------------------------------------------------------------------


class ChangeAction(StrEnum):
    """What applying a document would do with one of its settings."""

    WRITE = "write"
    UNCHANGED = "unchanged"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class SettingChange:
    """One setting of a document, against the analyzer."""

    name: str
    action: ChangeAction
    desired: DesiredSetting
    current: RegisterValue | None
    """The analyzer's value; ``None`` for a name that is not a holding register."""
    safety: SafetyTier
    """The tier of the write; ``READ_ONLY`` for one that will not be written."""
    reason: str | None = None
    """Why it is refused."""


@dataclass(frozen=True, slots=True)
class SettingsDiff:
    """Every setting of a document against the analyzer, writes in the order they would go."""

    changes: tuple[SettingChange, ...]
    identity_mismatch: str | None = None
    """Why the document is not this analyzer's, unless any analyzer was allowed."""

    @property
    def writes(self) -> tuple[SettingChange, ...]:
        """The settings that would be written, in order."""
        return tuple(c for c in self.changes if c.action is ChangeAction.WRITE)

    @property
    def refused(self) -> tuple[SettingChange, ...]:
        """The settings refused."""
        return tuple(c for c in self.changes if c.action is ChangeAction.REFUSED)

    @property
    def unchanged(self) -> tuple[SettingChange, ...]:
        """The settings the analyzer has already."""
        return tuple(c for c in self.changes if c.action is ChangeAction.UNCHANGED)

    @property
    def tier(self) -> SafetyTier:
        """The highest tier of the writes; ``READ_ONLY`` when there are none."""
        return max((c.safety for c in self.writes), default=SafetyTier.READ_ONLY)

    @property
    def ok(self) -> bool:
        """Whether the document may be applied: nothing refused, and it is this analyzer's."""
        return not self.refused and self.identity_mismatch is None

    def refusal(self) -> FujiValidationError:
        """The error for a document that may not be applied, naming every reason."""
        reasons = [f"{c.name}: {c.reason}" for c in self.refused]
        if self.identity_mismatch is not None:
            reasons.insert(0, self.identity_mismatch)
        msg = "the settings document is refused, nothing was written: " + "; ".join(reasons)
        return FujiValidationError(msg, context=ErrorContext(extra={"refused": tuple(reasons)}))


def diff_settings(
    document: SettingsDocument,
    current: Mapping[str, RegisterValue],
    *,
    registry: RegisterRegistry,
    ranges: Sequence[RangeInfo],
    serial_number: str | None,
    any_analyzer: bool = False,
) -> SettingsDiff:
    """Compare ``document`` with the analyzer's ``current`` holding registers.

    ``ranges`` are the range tables read with them; ``serial_number`` is the
    analyzer's. See the module docstring for what is refused.
    """
    against = _Against(current, registry, ranges, document)
    changes = [_change(name, desired, against) for name, desired in document.settings.items()]
    mismatch = None
    if (
        not any_analyzer
        and document.serial_number is not None
        and document.serial_number != serial_number
    ):
        mismatch = (
            f"the document describes the analyzer with serial number "
            f"{document.serial_number!r}, not this one ({serial_number!r})"
        )
    ordered = sorted(changes, key=lambda c: _order(c, registry))
    return SettingsDiff(tuple(ordered), mismatch)


@dataclass(frozen=True, slots=True)
class _Against:
    """What a document's settings are compared with."""

    current: Mapping[str, RegisterValue]
    registry: RegisterRegistry
    ranges: Sequence[RangeInfo]
    document: SettingsDocument


def _change(name: str, desired: DesiredSetting, against: _Against) -> SettingChange:
    spec = against.registry.resolve(name) if against.registry.has(name) else None
    value = against.current.get(name) if spec is not None else None
    reason = _not_a_setting(name, spec, value)
    if reason is None:
        assert spec is not None  # noqa: S101 - it is a setting
        assert value is not None  # noqa: S101
        if _same(spec, value, desired, against.ranges):
            return SettingChange(name, ChangeAction.UNCHANGED, desired, value, SafetyTier.READ_ONLY)
        reason = _write_refusal(spec, desired, against)
        if reason is None:
            return SettingChange(name, ChangeAction.WRITE, desired, value, spec.safety)
    return SettingChange(name, ChangeAction.REFUSED, desired, value, SafetyTier.READ_ONLY, reason)


def _not_a_setting(name: str, spec: RegisterSpec | None, value: RegisterValue | None) -> str | None:
    if name in OPERATIONS:
        return "an operation command, which a settings document never runs"
    if spec is None:
        return "not a register of the map"
    if spec.table is not RegisterTable.HOLDING or value is None:
        return "an input register, not a setting"
    return None


def _write_refusal(spec: RegisterSpec, desired: DesiredSetting, against: _Against) -> str | None:
    if not spec.writable:
        return "read-only, and the document gives another value"
    if desired.value is None:
        return "the document gives no value to write"
    try:
        prepared = prepare_value(spec, desired.value, unit=desired.unit)
        encode_prepared(prepared, _range_info(spec, against.ranges))
        _check_range_selection(spec, prepared.value, against)
    except FujiValidationError as exc:
        return str(exc.args[0]).removeprefix(f"{spec.name}: ")
    return None


def _same(
    spec: RegisterSpec,
    current: RegisterValue,
    desired: DesiredSetting,
    ranges: Sequence[RangeInfo],
) -> bool:
    if desired.value is None or (
        # An alarm limit's scaled value depends on the target channel the caller
        # gave when it was dumped; the word itself is what can be compared.
        spec.scaling.kind is ScalingKind.BY_ALARM_TARGET and desired.raw is not None
    ):
        return desired.raw is not None and desired.raw == current.raw
    return _units_match(spec, current, desired) and _values_match(
        spec, current, desired.value, ranges
    )


def _units_match(spec: RegisterSpec, current: RegisterValue, desired: DesiredSetting) -> bool:
    if spec.scaling.kind is ScalingKind.BY_RANGE and desired.unit is None:
        return False  # a scaled value means nothing without its unit
    if desired.unit is None or current.unit is None:
        return True
    if spec.unit is not None:
        return desired.unit.strip() == spec.unit
    return coerce_unit(desired.unit) is coerce_unit(current.unit)


def _values_match(
    spec: RegisterSpec,
    current: RegisterValue,
    wanted: bool | int | float | str,
    ranges: Sequence[RangeInfo],
) -> bool:
    shown = current.value
    if isinstance(shown, IntEnum):
        return isinstance(wanted, str) and _enum_key(wanted) == shown.name
    if isinstance(shown, bool) or isinstance(wanted, bool):
        return shown is wanted
    scaling = range_scaling(spec, ranges)
    if scaling is None:
        return wanted == current.raw
    try:
        exact = Decimal(repr(wanted)) if isinstance(wanted, float) else Decimal(wanted)
    except InvalidOperation:
        return False
    return exact == as_decimal(int(current.raw), scaling[0])


def _enum_key(text: str) -> str:
    return text.strip().upper().replace("-", "_").replace(" ", "_")


def _range_info(spec: RegisterSpec, ranges: Sequence[RangeInfo]) -> tuple[Unit, float, int] | None:
    for info in ranges:
        if info.channel is spec.channel and spec.range is not None:
            if spec.range > info.count:
                msg = f"{info.channel.value} has {info.count} range(s), so no range {spec.range}"
                raise FujiValidationError(msg)
            return info.of(spec.range)
    return None


_SELECTED_PREFIX: Final = "range.ch"


def _check_range_selection(spec: RegisterSpec, value: object, against: _Against) -> None:
    if not (spec.name.startswith(_SELECTED_PREFIX) and spec.name.endswith(".selected")):
        return
    assert spec.channel is not None  # noqa: S101 - a range setting belongs to a channel
    method_name = f"range.ch{spec.channel.number}.method"
    desired = against.document.settings.get(method_name)
    method: object = against.current[method_name].value
    if desired is not None and isinstance(desired.value, str):
        key = _enum_key(desired.value)
        method = RangeMethod[key] if key in RangeMethod.__members__ else desired.value
    if method is not RangeMethod.MANUAL:
        shown = method.name.lower() if isinstance(method, RangeMethod) else method
        msg = f"a range is selected only while {method_name} is manual, and it would be {shown}"
        raise FujiValidationError(msg)
    counts = {info.channel: info.count for info in against.ranges}
    if isinstance(value, RangeIndex) and value.number > counts[spec.channel]:
        msg = (
            f"{spec.channel.value} has {counts[spec.channel]} range(s), so no range {value.number}"
        )
        raise FujiValidationError(msg)


def _order(change: SettingChange, registry: RegisterRegistry) -> tuple[int, int]:
    """Writes first, in dependency order, then the rest in map order."""
    if change.action is not ChangeAction.WRITE:
        rank = 9
    elif change.name == "output_hold.enabled":
        rank = 0 if change.desired.value is True else 2
    elif change.name.startswith("hold."):
        rank = 1
    elif change.name.endswith(".method"):
        rank = 3
    elif change.name.endswith(".selected"):
        rank = 4
    elif change.name.startswith("response_time."):
        rank = 5
    elif change.name.startswith("calibration."):
        rank = 6
    else:
        rank = 7
    address = registry.resolve(change.name).address if registry.has(change.name) else 0xFFFF
    return (rank, address)


# --- Applying ----------------------------------------------------------------------------------


class ApplyStatus(StrEnum):
    """How applying a document ended."""

    OK = "ok"
    """Every write verified (or there was nothing to write)."""
    PARTIAL = "partial"
    """A write failed after others had completed; the rest were not attempted."""
    VERIFY_FAILED = "verify_failed"
    """A write read back as something else."""
    UNKNOWN = "unknown"
    """A write's outcome could not be established."""
    FAILED = "failed"
    """The first write failed, or was refused by the analyzer; nothing was changed."""


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """What applying a settings document did."""

    diff: SettingsDiff
    completed: tuple[WriteResult, ...]
    """The writes that completed, verified, in order."""
    failed: str | None = None
    """The setting whose write failed, if one did."""
    error: FujiError | None = None
    not_attempted: tuple[str, ...] = ()
    """The writes after the failed one."""

    @property
    def status(self) -> ApplyStatus:
        """How it ended (see :class:`ApplyStatus`)."""
        if self.error is None:
            return ApplyStatus.OK
        if isinstance(self.error, FujiVerificationError):
            return ApplyStatus.VERIFY_FAILED
        if isinstance(self.error, FujiWriteOutcomeUnknownError):
            return ApplyStatus.UNKNOWN
        return ApplyStatus.PARTIAL if self.completed else ApplyStatus.FAILED
