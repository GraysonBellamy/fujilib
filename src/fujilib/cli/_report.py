"""Models to plain report trees for the ``fuji-*`` commands.

Each function turns one model into dicts, lists and scalars that
:func:`fujilib.cli._common.render` prints as text or JSON, so every command
reports the same thing the same way whether it read a live analyzer or
decoded a register dump.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from fujilib.devices.capability import PROBED_CAPABILITIES
from fujilib.devices.settings import ChangeAction
from fujilib.devices.writes import describe

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import (
        AdcValues,
        AnalyzerMetadata,
        AnalyzerStatus,
        CalibrationLogEntry,
        ChannelInfo,
        DeviceInfo,
        ErrorLogEntry,
        Frame,
        RangeInfo,
    )
    from fujilib.devices.reads import ClockReading
    from fujilib.devices.settings import ApplyReport, SettingChange, SettingsDiff
    from fujilib.devices.snapshot import FujiDeviceSnapshot
    from fujilib.devices.writes import WriteResult
    from fujilib.registry.typecode import TypeCode

__all__ = [
    "adc_report",
    "apply_report",
    "calibration_log_report",
    "channels_report",
    "clock_report",
    "diff_report",
    "error_log_report",
    "frame_report",
    "info_report",
    "metadata_report",
    "parameter_report",
    "ranges_report",
    "settings_report",
    "show",
    "snapshot_report",
    "status_report",
    "type_code_report",
]


def show(value: object) -> object:
    """An enum member as its lower-case name; anything else unchanged."""
    if isinstance(value, Enum):
        return value.name.lower()
    return value


def type_code_report(type_code: TypeCode, serial_number: str) -> dict[str, object]:
    """The type code, the model it names, the digits the table lacks, and the serial number."""
    return {
        "type_code": type_code.raw,
        "model": type_code.model,
        "unknown_digits": list(type_code.unknown_digits),
        "serial_number": serial_number,
    }


def channels_report(channels: Sequence[ChannelInfo]) -> list[object]:
    """Each established channel's label and where it came from."""
    return [
        {
            "channel": c.channel.value,
            "gas": c.gas.value,
            "suggested_gas": c.suggested_gas.value if c.suggested_gas else None,
            "label_source": c.label_source.value,
            "role": c.role.value,
        }
        for c in channels
    ]


def info_report(info: DeviceInfo) -> dict[str, object]:
    """What ``identify()`` established."""
    return {
        **type_code_report(info.type_code, info.serial_number),
        "address": info.address,
        "port": info.serial_settings.port,
        "health": info.health.value,
        "availability": {
            str(c.name).lower(): info.availability[c].value
            for c in PROBED_CAPABILITIES
            if c in info.availability
        },
        "channels": channels_report(info.channels),
    }


def ranges_report(ranges: Sequence[RangeInfo]) -> list[object]:
    """Each measured channel's ranges, e.g. ``0-10.00 vol%``."""
    out: list[object] = []
    for info in ranges:
        described: list[str] = []
        for rng in range(1, info.count + 1):
            unit, full_scale, decimals = info.of(rng)
            described.append(f"0-{full_scale:.{decimals}f} {unit.value}")
        out.append({"channel": info.channel.value, "ranges": described})
    return out


def status_report(analyzer: AnalyzerStatus) -> dict[str, object]:
    """The analyzer-level status."""
    return {
        "instrument_error": analyzer.instrument_error,
        "calibration_error": analyzer.calibration_error,
        "errors": sorted(int(e) for e in analyzer.errors),
        "alarms": [show(a) for a in analyzer.alarms],
        "auto_calibration_running": analyzer.auto_calibration_running,
        "display": show(analyzer.display.screen) if analyzer.display else None,
    }


def frame_report(frame: Frame) -> dict[str, object]:
    """Each reading, with its gas and state, and the analyzer status when it was read."""
    readings = [
        {
            "channel": r.channel.value,
            "gas": r.gas.value,
            "value": f"{r.value:.{r.decimals}f} {r.unit.value}" if r.value is not None else None,
            "raw": r.raw_value,
            "state": r.state.value,
        }
        for r in frame.readings
    ]
    analyzer = status_report(frame.analyzer) if frame.analyzer is not None else None
    return {"channels": readings, "analyzer": analyzer}


def error_log_report(entries: Sequence[ErrorLogEntry]) -> list[object]:
    """The error log, newest first; the log keeps only day, hour and minute."""
    return [
        {
            "error": f"{int(e.code)} {show(e.code)}",
            "channel": e.channel.value if e.channel else None,
            "at": f"day {e.at.day} {e.at.hour:02d}:{e.at.minute:02d}",
        }
        for e in entries
    ]


def calibration_log_report(entries: Sequence[CalibrationLogEntry]) -> list[object]:
    """Calibration-log records, newest first per channel."""
    return [
        {
            "channel": e.channel.value,
            "kind": show(e.kind),
            "detector_count": e.detector_count,
            "deviation_percent_fs": e.deviation_percent_fs,
            "at": f"month {e.at.month} day {e.at.day} {e.at.hour:02d}:{e.at.minute:02d}",
        }
        for e in entries
    ]


def adc_report(adc: AdcValues) -> dict[str, object]:
    """The raw A/D counts, grouped as the service manual's table orders them."""
    return {
        "inputs": list(adc.inputs),
        "temperatures": list(adc.temperatures),
        "resistances": list(adc.resistances),
        "pressure": adc.pressure,
        "reference_voltage": adc.reference_voltage,
        "ground": adc.ground,
    }


def clock_report(clock: ClockReading) -> dict[str, object]:
    """The analyzer's clock and the host time it was read."""
    return {"clock": clock.clock.isoformat(sep=" "), "read_at": clock.read_at.isoformat()}


def metadata_report(meta: AnalyzerMetadata) -> dict[str, object]:
    """The settings a consumer records with its data."""
    schedule = meta.auto_calibration.schedule
    return {
        "response_time_s": {c.value: s for c, s in meta.response_time_s.items()},
        "response_time_ndir_s": list(meta.response_time_ndir_s),
        "response_time_o2_s": meta.response_time_o2_s,
        "calibration_gas": {
            f"{c.value} range {r}": list(pair) for (c, r), pair in meta.calibration_gas.items()
        },
        "hold_mode": show(meta.hold_mode),
        "output_hold": meta.output_hold,
        "auto_calibration": {
            "enabled": schedule.enabled,
            "start_day": show(schedule.start_day),
            "start_hour_raw": schedule.start_hour_raw,
            "start_minute_raw": schedule.start_minute_raw,
            "cycle": f"{schedule.cycle} {show(schedule.cycle_unit)}",
        },
        "auto_zero": {
            "enabled": meta.auto_zero.schedule.enabled,
            "flow_time_s": meta.auto_zero.flow_time_s,
        },
        "moving_average": [f"{a.period} {show(a.unit)}" for a in meta.moving_average],
    }


def parameter_report(value: RegisterValue) -> dict[str, object]:
    """One register: its decoded value, raw value and unit, and what may be done with it."""
    spec = value.spec
    return {
        "value": show(value.value),
        "raw": value.raw,
        "unit": value.unit,
        "access": spec.access.value,
        "safety": spec.safety.name.lower(),
        "evidence": spec.evidence.value,
    }


def snapshot_report(snapshot: FujiDeviceSnapshot) -> dict[str, object]:
    """Identity and health from cached state."""
    return {
        "name": snapshot.name,
        "model": snapshot.model,
        "serial": snapshot.serial,
        "connected": snapshot.connected,
        "recoverable_error_count": snapshot.recoverable_error_count,
        "last_error": snapshot.last_error.command_name if snapshot.last_error else None,
        "channels": [c.value for c in snapshot.channels],
        "captured_at": snapshot.captured_at.isoformat(),
    }


def settings_report(values: Mapping[str, RegisterValue]) -> dict[str, object]:
    """Every register of a settings read, by name."""
    return {name: parameter_report(value) for name, value in values.items()}


def _wanted(change: SettingChange) -> str:
    desired = change.desired
    shown = desired.value if desired.value is not None else f"raw {desired.raw}"
    return f"{shown} {desired.unit}" if desired.unit else str(shown)


def change_report(change: SettingChange) -> dict[str, object]:
    """One setting of a document against the analyzer."""
    out: dict[str, object] = {
        "action": change.action.value,
        "current": describe(change.current) if change.current is not None else None,
        "wanted": _wanted(change),
    }
    if change.action is ChangeAction.WRITE:
        out["safety"] = change.safety.name.lower()
    if change.reason is not None:
        out["reason"] = change.reason
    return out


def diff_report(diff: SettingsDiff) -> dict[str, object]:
    """A document against the analyzer: the writes in order, the refusals, a count of the rest."""
    out: dict[str, object] = {}
    if diff.identity_mismatch is not None:
        out["analyzer"] = diff.identity_mismatch
    out["write"] = {c.name: change_report(c) for c in diff.writes}
    out["refused"] = {c.name: change_report(c) for c in diff.refused}
    out["unchanged"] = len(diff.unchanged)
    out["tier"] = diff.tier.name.lower()
    return out


def write_report(result: WriteResult) -> dict[str, object]:
    """One setting write and what its read-back found."""
    return {
        "before": describe(result.previous),
        "written": describe(result.requested),
        "read_back": describe(result.observed) if result.observed is not None else None,
        "state": result.state.value,
        "acknowledged": result.acknowledged,
    }


def apply_report(report: ApplyReport) -> dict[str, object]:
    """What applying a document did, ending with its status."""
    out = diff_report(report.diff)
    out["written"] = {r.name: write_report(r) for r in report.completed}
    if report.failed is not None:
        out["failed"] = {"setting": report.failed, "error": str(report.error)}
        out["not_attempted"] = list(report.not_attempted)
    out["status"] = report.status.value
    return out
