"""A CSV file sink: one header line, then one line per sample.

- The header is the locked columns (:func:`~fujilib.sinks.base.row_columns`),
  written when the columns are known: at :meth:`CsvSink.open` if the sink
  was given its channels, otherwise with the first batch.
- Text is quoted and numbers are not, so ``None`` (an empty field) and empty
  text (``""``) stay apart: Python's :mod:`csv` reader with
  ``quoting=csv.QUOTE_NOTNULL`` reads them back as ``None`` and ``""``. That
  matters for ``chN_errors``, which is ``""`` for "no errors" and ``None``
  when unknown. Booleans are the text ``true`` / ``false``; floats are
  written so they read back exactly; the file is UTF-8.
- The file is flushed after every write, so a process that dies loses at
  most the batch it was writing. An existing file is replaced.
- File I/O runs in a worker thread, never on the event loop.
"""

from __future__ import annotations

import csv
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING

from fujilib.errors import FujiSinkWriteError
from fujilib.sinks._thread import in_thread
from fujilib.sinks.base import BaseSink

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from os import PathLike

    from fujilib.devices.models import Scalar
    from fujilib.registry.channels import ChannelId
    from fujilib.streaming.sample import Sample

__all__ = ["CsvSink", "csv_cell"]


def csv_cell(value: Scalar) -> str | int | float | None:
    """A row value as the CSV writer takes it: a boolean becomes ``true`` / ``false``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class CsvSink(BaseSink):
    """Write rows to a CSV file (see the module docstring)."""

    def __init__(
        self, path: str | PathLike[str], *, channels: Iterable[ChannelId] | None = None
    ) -> None:
        """A sink for ``path``; its columns are locked now if ``channels`` are given."""
        super().__init__(f"csv:{path}", channels)
        self._path = Path(path)
        self._file: IO[str] | None = None
        self._header_written = False

    @property
    def path(self) -> Path:
        """The file written."""
        return self._path

    async def _open(self) -> None:
        try:
            self._file = await in_thread(self._open_file)
        except OSError as exc:
            msg = f"cannot open {str(self._path)!r} for writing: {exc}"
            raise FujiSinkWriteError(msg) from exc
        if self.schema.is_locked:
            try:
                await self._write_lines([])
            except BaseException:
                await self._close()
                raise

    def _open_file(self) -> IO[str]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return self._path.open("w", encoding="utf-8", newline="")

    async def _write(self, samples: Sequence[Sample], rows: list[dict[str, Scalar]]) -> None:
        del samples
        await self._write_lines(rows)

    async def _write_lines(self, rows: list[dict[str, Scalar]]) -> None:
        try:
            await in_thread(partial(self._write_rows, rows))
        except OSError as exc:
            msg = f"cannot write to {str(self._path)!r}: {exc}"
            raise FujiSinkWriteError(msg) from exc

    def _write_rows(self, rows: list[dict[str, Scalar]]) -> None:
        file = self._file
        assert file is not None  # noqa: S101 - written only while open
        writer = csv.writer(file, lineterminator="\n", quoting=csv.QUOTE_STRINGS)
        if not self._header_written:
            writer.writerow([c.name for c in self.schema.columns])
            self._header_written = True
        names = [c.name for c in self.schema.columns]
        writer.writerows([csv_cell(row[name]) for name in names] for row in rows)
        file.flush()

    async def _close(self) -> None:
        file, self._file = self._file, None
        assert file is not None  # noqa: S101 - _close runs only after _open opened it
        try:
            await in_thread(file.close)
        except OSError as exc:
            msg = f"cannot finish {str(self._path)!r}: {exc}"
            raise FujiSinkWriteError(msg) from exc
