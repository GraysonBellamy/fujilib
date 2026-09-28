"""Column specifications for tabular sinks.

A sink fixes its schema before the first row from :func:`fujilib.sinks.base.row_columns`
rather than inferring types from the first batch, so a recording that starts
with an error row cannot lock every reading column as a string.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ColumnSpec"]


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column of a tabular schema.

    Attributes:
        name: Column name, verbatim from the row dict.
        python_type: The scalar type backing the column: :class:`float`,
            :class:`int`, :class:`str` or :class:`bool`.
        nullable: Whether the column may hold ``None``.
    """

    name: str
    python_type: type[float] | type[int] | type[str] | type[bool]
    nullable: bool
