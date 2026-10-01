"""The model builders of ``fujilib.testing.frames``, and the suite's own file helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fujilib.testing import BENCH_BANK_PATH
from fujilib.testing.frames import (
    MONO0,
    T0,
    analyzer,
    bench_readings,
    frame,
    reading,
    status,
    timing,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MONO0",
    "T0",
    "analyzer",
    "bench_banks",
    "bench_readings",
    "frame",
    "parquet_metadata",
    "parquet_table",
    "read_parquet",
    "reading",
    "status",
    "timing",
]


def bench_banks() -> tuple[dict[int, int], dict[int, int]]:
    """``(holding, input)`` banks of the committed, sanitized bench capture."""
    data = json.loads(BENCH_BANK_PATH.read_text(encoding="utf-8"))
    return (
        {int(a, 16): w for a, w in data["holding"].items()},
        {int(a, 16): w for a, w in data["input"].items()},
    )


def read_parquet(path: Path) -> list[dict[str, object]]:
    """A Parquet file's rows (``pyarrow``'s stubs leave ``read_table`` partly untyped)."""
    import pyarrow.parquet as pq

    table: Any = pq.read_table(path)  # pyright: ignore[reportUnknownMemberType]
    rows: list[dict[str, object]] = table.to_pylist()
    return rows


def parquet_table(path: Path) -> Any:
    """A Parquet file as a ``pyarrow`` table, untyped."""
    import pyarrow.parquet as pq

    return pq.read_table(path)  # pyright: ignore[reportUnknownMemberType]


def parquet_metadata(path: Path) -> dict[str, str]:
    """A Parquet file's key-value metadata, decoded."""
    raw: Any = parquet_table(path).schema.metadata or {}
    return {bytes(k).decode(): bytes(v).decode() for k, v in raw.items()}
