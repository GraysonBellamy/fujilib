"""Flatten a :class:`~fujilib.streaming.sample.Sample` into one wide row (design §7.6).

Row layout, fixed once the established channels are known:

- the header: ``device``, ``address``, ``protocol``, ``t_mono_ns``, ``t_utc``,
  ``t_midpoint_mono_ns``, ``requested_at``, ``received_at``, ``latency_s``;
- per established channel ``N``, the columns of
  :data:`~fujilib.devices.models.READING_COLUMNS` prefixed ``chN_``
  (``ch3_value``, ``ch3_state``, …);
- the analyzer columns of :data:`~fujilib.devices.models.ANALYZER_COLUMNS`;
- ``error_type`` and ``error_message``, ``None`` on a successful poll.

Every value is ``float``, ``int``, ``str``, ``bool`` or ``None``; datetimes are
ISO 8601 strings. A successful row and an error row have exactly the same keys:
an error row carries ``None`` in every reading and analyzer column.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fujilib.devices.models import ANALYZER_COLUMNS, READING_COLUMNS
from fujilib.sinks._schema import ColumnSpec

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fujilib.devices.models import Scalar
    from fujilib.registry.channels import ChannelId
    from fujilib.streaming.sample import Sample

__all__ = ["HEADER_COLUMNS", "row_columns", "sample_to_row"]

#: The header columns: ``(name, type, nullable)``.
HEADER_COLUMNS: Final[tuple[ColumnSpec, ...]] = (
    ColumnSpec("device", str, nullable=False),
    ColumnSpec("address", int, nullable=False),
    ColumnSpec("protocol", str, nullable=False),
    ColumnSpec("t_mono_ns", int, nullable=False),
    ColumnSpec("t_utc", str, nullable=False),
    ColumnSpec("t_midpoint_mono_ns", int, nullable=True),
    ColumnSpec("requested_at", str, nullable=False),
    ColumnSpec("received_at", str, nullable=False),
    ColumnSpec("latency_s", float, nullable=False),
)

_ERROR_COLUMNS: Final[tuple[ColumnSpec, ...]] = (
    ColumnSpec("error_type", str, nullable=True),
    ColumnSpec("error_message", str, nullable=True),
)


def _prefix(channel: ChannelId) -> str:
    return f"ch{channel.number}_"


def row_columns(channels: Iterable[ChannelId]) -> tuple[ColumnSpec, ...]:
    """The columns of every row for an analyzer with these established ``channels``.

    Reading and analyzer columns are nullable, because an error row carries
    ``None`` in all of them.
    """
    specs = list(HEADER_COLUMNS)
    for channel in channels:
        prefix = _prefix(channel)
        specs.extend(
            ColumnSpec(prefix + c.name, c.python_type, nullable=True) for c in READING_COLUMNS
        )
    specs.extend(ColumnSpec(c.name, c.python_type, nullable=True) for c in ANALYZER_COLUMNS)
    specs.extend(_ERROR_COLUMNS)
    return tuple(specs)


def sample_to_row(
    sample: Sample,
    channels: Iterable[ChannelId] | None = None,
) -> dict[str, Scalar]:
    """Flatten ``sample`` into one wide row; see the module docstring for the layout.

    Args:
        sample: The sample to flatten.
        channels: The established channels, which fix the row's columns. When
            omitted, the frame's own channels are used; an error sample then
            has no channel columns, so a recorder always passes them.
    """
    frame = sample.frame
    established = tuple(channels) if channels is not None else (frame.channels if frame else ())
    row: dict[str, Scalar] = {
        "device": sample.device,
        "address": sample.address,
        "protocol": sample.protocol.value,
        "t_mono_ns": sample.t_mono_ns,
        "t_utc": sample.t_utc.isoformat(),
        "t_midpoint_mono_ns": sample.t_midpoint_mono_ns,
        "requested_at": sample.requested_at.isoformat(),
        "received_at": sample.received_at.isoformat(),
        "latency_s": sample.latency_s,
    }
    readings = {r.channel: r for r in frame.readings} if frame is not None else {}
    for channel in established:
        prefix = _prefix(channel)
        reading = readings.get(channel)
        values = reading.as_dict() if reading is not None else {}
        row.update({prefix + c.name: values.get(c.name) for c in READING_COLUMNS})
    analyzer = frame.analyzer.as_dict() if frame is not None and frame.analyzer is not None else {}
    row.update({c.name: analyzer.get(c.name) for c in ANALYZER_COLUMNS})
    error = sample.error
    if error is not None:
        cls = type(error)
        row["error_type"] = f"{cls.__module__}.{cls.__qualname__}"
        row["error_message"] = str(error)
    else:
        row["error_type"] = None
        row["error_message"] = None
    return row
