"""Recover the rows of a ``fuji-capture`` Parquet file whose process was killed.

A Parquet file is readable only once its footer is written, when it is
closed. A process that is killed (its window closed, the power lost) leaves
the row groups it had written, each 1,000 rows, but no footer. This script
walks the page headers of the complete row groups, builds the footer
``pyarrow`` would have written for them, and writes a new, readable file; the
original is not changed. Rows that were waiting for their row group are lost;
a shorter last group, written as the file was being closed, is kept.

The columns come from the capture's ``.meta.json`` (its ``channels``), or
from ``--channels`` for a file written by ``ParquetSink`` directly. The file
must have been written as ``ParquetSink`` writes: zstd (or ``--compression``),
dictionary encoding, ``--row-group-size`` rows per group.

Before it touches the file, the script writes a small file of the same
columns with ``pyarrow``, rebuilds that file's footer the same way and checks
it against the real one. The recovered file is read back in full before the
script reports success.

    uv run python scripts/recover_parquet.py probe_out/soak.parquet

writes ``probe_out/soak.recovered.parquet`` and, beside it, a copy of the
``.meta.json`` with a ``recovered`` entry, so ``scripts/check_soak.py`` reads
it as it reads any capture. The file's key-value metadata gains
``fujilib.recovered``.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from fujilib.registry.channels import ChannelId
from fujilib.sinks import row_columns
from fujilib.sinks.parquet import DEFAULT_ROW_GROUP_SIZE

MAGIC = b"PAR1"

# --- Thrift compact protocol ------------------------------------------------------------------
# Generic: a struct is a list of (field id, type, value); a list is (element type, items).

BOOL_TRUE, BOOL_FALSE, BYTE, I16, I32, I64, DOUBLE, BINARY, LIST, SET, MAP, STRUCT = range(1, 13)
type Struct = list[tuple[int, int, Any]]


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def write_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def read_value(buf: bytes, pos: int, kind: int) -> tuple[Any, int]:
    if kind in {I16, I32, I64}:
        n, pos = read_varint(buf, pos)
        return unzigzag(n), pos
    if kind == BYTE:
        return struct.unpack_from("<b", buf, pos)[0], pos + 1
    if kind == DOUBLE:
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if kind == BINARY:
        n, pos = read_varint(buf, pos)
        return bytes(buf[pos : pos + n]), pos + n
    if kind in {LIST, SET}:
        head = buf[pos]
        pos += 1
        size, element = head >> 4, head & 0x0F
        if size == 15:
            size, pos = read_varint(buf, pos)
        items = []
        for _ in range(size):
            if element in {BOOL_TRUE, BOOL_FALSE}:
                items.append(buf[pos] == 1)
                pos += 1
            else:
                item, pos = read_value(buf, pos, element)
                items.append(item)
        return (element, items), pos
    if kind == STRUCT:
        return read_struct(buf, pos)
    msg = f"unsupported Thrift type {kind} at byte {pos}"
    raise ValueError(msg)


def read_struct(buf: bytes, pos: int) -> tuple[Struct, int]:
    fields: Struct = []
    last = 0
    while True:
        head = buf[pos]
        pos += 1
        if head == 0:
            return fields, pos
        delta, kind = head >> 4, head & 0x0F
        if delta:
            field = last + delta
        else:
            n, pos = read_varint(buf, pos)
            field = unzigzag(n)
        if kind in {BOOL_TRUE, BOOL_FALSE}:
            fields.append((field, kind, kind == BOOL_TRUE))
        else:
            value, pos = read_value(buf, pos, kind)
            fields.append((field, kind, value))
        last = field


def write_value(kind: int, value: Any) -> bytes:
    if kind in {I16, I32, I64}:
        return write_varint(zigzag(value))
    if kind == BYTE:
        return struct.pack("<b", value)
    if kind == DOUBLE:
        return struct.pack("<d", value)
    if kind == BINARY:
        return write_varint(len(value)) + value
    if kind in {LIST, SET}:
        element, items = value
        out = bytearray()
        if len(items) < 15:
            out.append((len(items) << 4) | element)
        else:
            out += bytes([0xF0 | element]) + write_varint(len(items))
        for item in items:
            if element in {BOOL_TRUE, BOOL_FALSE}:
                out.append(1 if item else 2)
            else:
                out += write_value(element, item)
        return bytes(out)
    if kind == STRUCT:
        return write_struct(value)
    msg = f"unsupported Thrift type {kind}"
    raise ValueError(msg)


def write_struct(fields: Struct) -> bytes:
    out = bytearray()
    last = 0
    for field, kind, value in fields:
        wire = (BOOL_TRUE if value else BOOL_FALSE) if kind in {BOOL_TRUE, BOOL_FALSE} else kind
        delta = field - last
        if 0 < delta <= 15:
            out.append((delta << 4) | wire)
        else:
            out.append(wire)
            out += write_varint(zigzag(field))
        if kind not in {BOOL_TRUE, BOOL_FALSE}:
            out += write_value(kind, value)
        last = field
    out.append(0)
    return bytes(out)


def get(fields: Struct, field: int, default: Any = None) -> Any:
    return next((value for f, _kind, value in fields if f == field), default)


# --- Parquet ----------------------------------------------------------------------------------
# Field numbers are those of parquet.thrift: FileMetaData, RowGroup, ColumnChunk,
# ColumnMetaData, PageHeader, DataPageHeader, DictionaryPageHeader, PageEncodingStats.

DATA_PAGE, DICTIONARY_PAGE = 0, 2


def footer_of(data: bytes) -> tuple[bytes, int]:
    """A complete file's footer, and the byte where it starts."""
    length = struct.unpack_from("<i", data, len(data) - 8)[0]
    start = len(data) - 8 - length
    return data[start : len(data) - 8], start


def scan(buf: bytes, columns: int, rows: int, end: int) -> list[list[dict[str, Any]]]:
    """The complete row groups from byte 4 on: per column chunk, offsets, sizes, encodings.

    Every group holds ``rows`` rows but a last, shorter one, written as the file
    was being closed; the first column's next dictionary page marks where that
    one's chunk ends. A group the file ends inside is left out.
    """
    groups: list[list[dict[str, Any]]] = []
    pos = 4
    while True:
        group: list[dict[str, Any]] = []
        try:
            for column in range(columns):
                want = group[0]["values"] if group else rows
                chunk: dict[str, Any] = {
                    "start": pos,
                    "dictionary": None,
                    "data": None,
                    "values": 0,
                    "compressed": 0,
                    "uncompressed": 0,
                    "encodings": [],
                    "pages": {},
                }
                while chunk["values"] < want:
                    header, body = read_struct(buf, pos)
                    kind, size, stored = get(header, 1), get(header, 2), get(header, 3)
                    if kind == DICTIONARY_PAGE and chunk["data"] is not None and column == 0:
                        want = chunk["values"]  # the next column's chunk: the shorter last group
                        break
                    if body + stored > end:
                        raise EOFError
                    chunk["compressed"] += body - pos + stored
                    chunk["uncompressed"] += body - pos + size
                    if kind == DICTIONARY_PAGE and chunk["data"] is None:
                        chunk["dictionary"] = pos
                        encoding = get(get(header, 7), 2)
                        chunk["encodings"].append(encoding)
                    elif kind == DATA_PAGE:
                        page = get(header, 5)
                        chunk["data"] = pos if chunk["data"] is None else chunk["data"]
                        chunk["values"] += get(page, 1)
                        encoding = get(page, 2)
                        chunk["encodings"] += [get(page, 3), encoding]  # levels, values
                    else:
                        msg = f"unexpected page type {kind} at byte {pos}"
                        raise ValueError(msg)
                    chunk["pages"][kind, encoding] = chunk["pages"].get((kind, encoding), 0) + 1
                    pos = body + stored
                if chunk["values"] != want:
                    msg = (
                        f"the column chunk at byte {chunk['start']} holds {chunk['values']} values"
                    )
                    raise ValueError(msg)
                group.append(chunk)
        except (EOFError, IndexError):
            return groups  # the file ends inside this group
        groups.append(group)
        if group[0]["values"] < rows:
            return groups


def group_rows(group: list[dict[str, Any]]) -> int:
    return int(group[0]["values"])


def build_footer(template: Struct, groups: list[list[dict[str, Any]]]) -> bytes:
    """``template``'s footer (schema, key-value metadata, writer) over ``groups``."""
    model = get(template, 4)[1][0]  # the first row group: each column's type, path, codec
    row_groups = []
    for group in groups:
        chunks = []
        for chunk, model_chunk in zip(group, get(model, 1)[1], strict=True):
            model_meta = get(model_chunk, 3)
            order = get(model_meta, 2)[1]
            encodings = sorted(
                set(chunk["encodings"]), key=lambda e: order.index(e) if e in order else len(order)
            )
            meta: Struct = [
                (1, I32, get(model_meta, 1)),
                (2, LIST, (I32, encodings)),
                (3, LIST, get(model_meta, 3)),
                (4, I32, get(model_meta, 4)),
                (5, I64, chunk["values"]),
                (6, I64, chunk["uncompressed"]),
                (7, I64, chunk["compressed"]),
                (9, I64, chunk["data"]),
            ]
            if chunk["dictionary"] is not None:
                meta.append((11, I64, chunk["dictionary"]))
            stats = [
                [(1, I32, kind), (2, I32, encoding), (3, I32, count)]
                for (kind, encoding), count in chunk["pages"].items()
            ]
            meta.append((13, LIST, (STRUCT, stats)))
            # pyarrow writes 0 here; a writer that gives the chunk's first page gets ours.
            offset = get(model_chunk, 2)
            if offset and offset == get(model_meta, 11, get(model_meta, 9)):
                offset = chunk["start"]
            chunks.append([(2, I64, offset), (3, STRUCT, meta)])
        row_groups.append(
            [
                (1, LIST, (STRUCT, chunks)),
                (2, I64, sum(c["uncompressed"] for c in group)),
                (3, I64, group_rows(group)),
                (5, I64, group[0]["start"]),
                (6, I64, sum(c["compressed"] for c in group)),
            ]
        )
    footer: Struct = []
    for field, kind, value in template:
        if field == 3:
            footer.append((3, I64, sum(group_rows(g) for g in groups)))
        elif field == 4:
            footer.append((4, LIST, (STRUCT, row_groups)))
        else:
            footer.append((field, kind, value))
    return write_struct(footer)


def without_statistics(footer: Struct) -> Struct:
    """``footer`` less what the rebuild leaves out: statistics and page indexes."""

    def chunk(fields: Struct) -> Struct:
        out: Struct = []
        for f, k, v in fields:
            if f == 3:
                meta = [(g, j, w) for g, j, w in v if g not in {12, 16}]
                meta = [
                    (g, j, (w[0], sorted(map(repr, w[1]))) if g == 13 else w) for g, j, w in meta
                ]
                out.append((f, k, meta))
            elif f not in {4, 5, 6, 7}:
                out.append((f, k, v))
        return out

    groups = [
        [(f, k, (v[0], [chunk(c) for c in v[1]]) if f == 1 else v) for f, k, v in group]
        for group in get(footer, 4)[1]
    ]
    return [(f, k, (STRUCT, groups) if f == 4 else v) for f, k, v in footer]


# --- The recovery ----------------------------------------------------------------------------

_ARROW_TYPES = {bool: pa.bool_(), int: pa.int64(), float: pa.float64(), str: pa.string()}


def arrow_schema(channels: list[ChannelId], metadata: dict[str, str]) -> pa.Schema:
    """The schema ``ParquetSink`` writes for these channels."""
    return pa.schema(
        [
            pa.field(c.name, _ARROW_TYPES[c.python_type], nullable=c.nullable)
            for c in row_columns(channels)
        ],
        metadata=metadata,
    )


def template_footer(
    schema: pa.Schema, total: int, rows: int, compression: str
) -> tuple[Struct, bytes, int]:
    """A file of ``total`` rows in groups of ``rows``: its parsed footer, bytes and footer start."""
    samples = {pa.bool_(): True, pa.int64(): 1, pa.float64(): 1.5, pa.string(): "x"}
    table = pa.table(
        {f.name: pa.array([samples[f.type]] * total, f.type) for f in schema},
        schema=schema,
    )
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "template.parquet"
        with pq.ParquetWriter(path, schema, compression=compression, use_dictionary=True) as w:
            w.write_table(table, row_group_size=rows)
        data = path.read_bytes()
    raw, start = footer_of(data)
    parsed, _ = read_struct(raw, 0)
    if write_struct(parsed) != raw:
        msg = "the Thrift codec does not reproduce pyarrow's footer"
        raise RuntimeError(msg)
    return parsed, data, start


def self_test(schema: pa.Schema, rows: int, compression: str) -> None:
    """Rebuild a template file's footer and check it against the real one.

    Two whole row groups and a shorter last one, as ``ParquetSink`` leaves on closing.
    """
    template, data, start = template_footer(schema, 2 * rows + rows // 5, rows, compression)
    groups = scan(data, len(schema), rows, start)
    rebuilt, _ = read_struct(build_footer(template, groups), 0)
    if without_statistics(rebuilt) != without_statistics(template):
        msg = "the rebuilt footer of a template file differs from pyarrow's; nothing was written"
        raise RuntimeError(msg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data", help="the Parquet file whose process was killed")
    parser.add_argument("--out", help="the file to write (default: <data>.recovered.parquet)")
    parser.add_argument("--channels", help="e.g. CH1,CH2,CH3 (default: from the .meta.json)")
    parser.add_argument("--row-group-size", type=int, default=DEFAULT_ROW_GROUP_SIZE)
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--force", action="store_true", help="replace --out if it exists")
    args = parser.parse_args(argv)
    data = Path(args.data)
    out = Path(args.out) if args.out else data.with_name(data.stem + ".recovered" + data.suffix)
    sidecar = Path(str(data) + ".meta.json")
    document = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None
    if args.channels:
        names = args.channels.split(",")
    elif document is not None:
        names = document["channels"]
    else:
        parser.error(f"{sidecar} is missing; give --channels")
    for path in (out, Path(str(out) + ".meta.json")):
        if path.exists() and not args.force:
            parser.error(f"{path} exists; give --force to replace it")
    buf = data.read_bytes()
    if buf[:4] != MAGIC:
        parser.error(f"{data} is not a Parquet file")
    if buf[-4:] == MAGIC:
        parser.error(f"{data} has its footer; it needs no recovery")

    rows = args.row_group_size
    channels = [ChannelId(n) for n in names]
    self_test(arrow_schema(channels, {}), rows, args.compression)

    groups = scan(buf, len(row_columns(channels)), rows, len(buf))
    if not groups:
        print(f"{data} holds no complete row group; nothing to recover")
        return 1
    end = groups[-1][-1]["start"] + groups[-1][-1]["compressed"]
    recovered = {
        "from": str(data),
        "at": datetime.now(UTC).isoformat(),
        "row_groups": len(groups),
        "rows": sum(group_rows(g) for g in groups),
        "bytes_used": end,
        "bytes_dropped": len(buf) - end,
    }
    metadata = {"fujilib.recovered": json.dumps(recovered)}
    if document is not None:
        metadata = {
            "fujilib.version": document["versions"]["fujilib"],
            "fujilib.capture": json.dumps(document, default=str),
            **metadata,
        }
    template, _data, _start = template_footer(
        arrow_schema(channels, metadata), rows, rows, args.compression
    )
    footer = build_footer(template, groups)
    out.write_bytes(buf[:end] + footer + struct.pack("<i", len(footer)) + MAGIC)
    table = pq.read_table(out)  # every page, decompressed and decoded
    if table.num_rows != recovered["rows"]:
        msg = f"read back {table.num_rows} rows, expected {recovered['rows']}"
        raise RuntimeError(msg)
    if document is not None:
        copy = {**document, "recovered": recovered}
        Path(str(out) + ".meta.json").write_text(
            json.dumps(copy, indent=2) + "\n", encoding="utf-8"
        )
    print(
        f"recovered {recovered['rows']} rows in {len(groups)} row groups to {out}; "
        f"{recovered['bytes_dropped']} bytes of an unfinished row group were left out"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
