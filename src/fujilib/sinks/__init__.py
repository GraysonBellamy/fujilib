"""Rows and sinks for recordings (design §7.6).

- :func:`sample_to_row` and :func:`row_columns` — the wide row and its columns.
- :class:`InMemorySink`, :class:`CsvSink` and :class:`ParquetSink` (the
  ``parquet`` extra) — where rows go; :func:`pipe` writes a recording to one.
"""

from __future__ import annotations

from fujilib.sinks._schema import ColumnSpec
from fujilib.sinks.base import (
    HEADER_COLUMNS,
    BaseSink,
    SampleSink,
    SchemaLock,
    pipe,
    row_columns,
    sample_channels,
    sample_to_row,
)
from fujilib.sinks.csv import CsvSink
from fujilib.sinks.memory import InMemorySink
from fujilib.sinks.parquet import ParquetSink

__all__ = [
    "HEADER_COLUMNS",
    "BaseSink",
    "ColumnSpec",
    "CsvSink",
    "InMemorySink",
    "ParquetSink",
    "SampleSink",
    "SchemaLock",
    "pipe",
    "row_columns",
    "sample_channels",
    "sample_to_row",
]
