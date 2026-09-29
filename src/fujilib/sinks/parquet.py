"""A Parquet file sink; needs the ``parquet`` extra (``pip install 'fujilib[parquet]'``).

- The Arrow schema is the locked columns
  (:func:`~fujilib.sinks.base.row_columns`): ``float64``, ``int64``,
  ``bool`` and ``string``, nullable exactly where a row can hold ``None``.
  It is fixed when the columns are: at :meth:`ParquetSink.open` if the sink
  was given its channels, otherwise with the first batch.
- Rows are gathered into row groups of ``row_group_size`` rows (1,000 by
  default), whatever the size of each ``write_many``, and the last, shorter
  group is written on close. ``pipe()`` writes about once a second, so one row
  group per write would make a day's recording at 1 Hz 86,400 row groups, whose
  metadata ``pyarrow`` keeps in memory until the file is closed. Rows waiting
  for their group are held as Arrow data.
- The file's key-value metadata carries ``fujilib.version`` and whatever the
  caller passes as ``metadata``.
- A Parquet file is only readable once its footer is written, by
  :meth:`~fujilib.sinks.base.BaseSink.close`. Closing runs on cancellation and
  on Ctrl-C, but a process that is killed leaves an unreadable file; use the
  CSV sink where that matters.
- ``pyarrow`` is imported when the sink opens, so fujilib imports without it.
  All ``pyarrow`` work runs in a worker thread, never on the event loop.
"""

from __future__ import annotations

import importlib
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from fujilib.errors import FujiSinkDependencyError, FujiSinkWriteError, FujiValidationError
from fujilib.sinks._thread import in_thread
from fujilib.sinks.base import BaseSink
from fujilib.version import __version__

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from os import PathLike

    import pyarrow as pa
    import pyarrow.parquet as pq

    from fujilib.devices.models import Scalar
    from fujilib.registry.channels import ChannelId
    from fujilib.sinks._schema import ColumnSpec
    from fujilib.streaming.sample import Sample

__all__ = [
    "COMPRESSIONS",
    "DEFAULT_ROW_GROUP_SIZE",
    "Compression",
    "ParquetSink",
    "require_pyarrow",
]

type Compression = Literal["zstd", "snappy", "gzip", "brotli", "lz4", "none"]
"""A Parquet compression codec."""

#: Rows per row group unless the caller says otherwise.
DEFAULT_ROW_GROUP_SIZE: Final = 1000

#: The compression codecs a :class:`ParquetSink` accepts.
COMPRESSIONS: Final[frozenset[str]] = frozenset({"zstd", "snappy", "gzip", "brotli", "lz4", "none"})

_INSTALL_HINT: Final = "install it with: pip install 'fujilib[parquet]'"


def require_pyarrow() -> None:
    """Check that ``pyarrow`` can be imported.

    Raises:
        FujiSinkDependencyError: it cannot; the ``parquet`` extra is missing.
    """
    try:
        _ = importlib.import_module("pyarrow")
        _ = importlib.import_module("pyarrow.parquet")
    except ImportError as exc:
        msg = f"the Parquet sink needs pyarrow, which is not installed; {_INSTALL_HINT}"
        raise FujiSinkDependencyError(msg) from exc


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _arrow_type(column: ColumnSpec) -> pa.DataType:
    import pyarrow as pa  # noqa: PLC0415 - optional

    if column.python_type is bool:
        return pa.bool_()
    if column.python_type is int:
        return pa.int64()
    if column.python_type is float:
        return pa.float64()
    return pa.string()


class ParquetSink(BaseSink):
    """Write rows to a Parquet file (see the module docstring)."""

    def __init__(
        self,
        path: str | PathLike[str],
        *,
        channels: Iterable[ChannelId] | None = None,
        compression: Compression = "zstd",
        row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """A sink for ``path``; its columns are locked now if ``channels`` are given.

        Args:
            path: The file to write.
            channels: The channels whose columns to write.
            compression: The codec.
            row_group_size: Rows per row group.
            metadata: Key-value metadata for the file, text to text.

        Raises:
            FujiValidationError: an unknown ``compression``, a ``row_group_size``
                below 1, or ``metadata`` that is not text.
        """
        if compression not in COMPRESSIONS:
            known = ", ".join(sorted(COMPRESSIONS))
            msg = f"compression must be one of {known}, got {compression!r}"
            raise FujiValidationError(msg)
        if not _is_count(row_group_size) or row_group_size < 1:
            msg = f"row_group_size must be an integer of at least 1, got {row_group_size!r}"
            raise FujiValidationError(msg)
        extra = dict(metadata or {})
        if not all(_is_text(k) and _is_text(v) for k, v in extra.items()):
            msg = "metadata must map text to text"
            raise FujiValidationError(msg)
        super().__init__(f"parquet:{path}", channels)
        self._path = Path(path)
        self._compression: Compression = compression
        self._row_group_size = row_group_size
        self._metadata: Mapping[str, str] = MappingProxyType(
            {"fujilib.version": __version__, **extra}
        )
        self._writer: pq.ParquetWriter | None = None
        self._arrow_schema: pa.Schema | None = None
        self._waiting: list[pa.Table] = []
        self._waiting_rows = 0

    @property
    def path(self) -> Path:
        """The file written."""
        return self._path

    @property
    def metadata(self) -> Mapping[str, str]:
        """The file's key-value metadata."""
        return self._metadata

    async def _open(self) -> None:
        require_pyarrow()
        if self.schema.is_locked:
            await self._run(self._start, "open")

    def _start(self) -> None:
        """Create the writer for the locked columns."""
        import pyarrow as pa  # noqa: PLC0415 - optional
        import pyarrow.parquet as pq  # noqa: PLC0415

        metadata: dict[bytes | str, bytes | str] = dict(self._metadata.items())
        schema = pa.schema(
            [pa.field(c.name, _arrow_type(c), nullable=c.nullable) for c in self.schema.columns],
            metadata=metadata,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(
            self._path, schema, compression=self._compression, use_dictionary=True
        )
        self._arrow_schema = schema

    async def _write(self, samples: Sequence[Sample], rows: list[dict[str, Scalar]]) -> None:
        del samples
        await self._run(partial(self._write_rows, rows), "write to")

    def _write_rows(self, rows: list[dict[str, Scalar]]) -> None:
        import pyarrow as pa  # noqa: PLC0415 - optional

        if self._writer is None:
            self._start()
        writer, schema = self._writer, self._arrow_schema
        assert writer is not None  # noqa: S101 - _start set it
        assert schema is not None  # noqa: S101
        self._waiting.append(pa.Table.from_pylist(rows, schema=schema))
        self._waiting_rows += len(rows)
        if self._waiting_rows >= self._row_group_size:
            self._write_groups(final=False)

    def _write_groups(self, *, final: bool) -> None:
        """Write the whole row groups waiting, and on ``final`` the rest too."""
        import pyarrow as pa  # noqa: PLC0415 - optional

        writer = self._writer
        assert writer is not None  # noqa: S101 - rows wait only once it exists
        table = pa.concat_tables(self._waiting)
        size = self._row_group_size
        # Called with at least one whole group waiting, or at the end with some rows.
        whole = table.num_rows if final else table.num_rows - table.num_rows % size
        writer.write_table(table.slice(0, whole), row_group_size=size)
        rest = table.slice(whole)
        self._waiting = [rest] if rest.num_rows else []
        self._waiting_rows = rest.num_rows

    def _finish(self, writer: pq.ParquetWriter) -> None:
        if self._waiting:
            self._write_groups(final=True)
        writer.close()

    async def _close(self) -> None:
        writer = self._writer
        if writer is not None:
            try:
                await self._run(partial(self._finish, writer), "finish")
            finally:
                self._writer = None

    async def _run(self, func: Callable[[], None], verb: str) -> None:
        import pyarrow as pa  # noqa: PLC0415 - optional

        try:
            await in_thread(func)
        except (OSError, pa.ArrowException) as exc:
            msg = f"cannot {verb} {str(self._path)!r}: {exc}"
            raise FujiSinkWriteError(msg) from exc
