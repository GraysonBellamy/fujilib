"""The Parquet sink (design §7.6); the ``parquet`` extra is installed for tests."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fujilib import (
    FujiConfigurationError,
    FujiModbusTimeoutError,
    FujiSinkDependencyError,
    FujiSinkWriteError,
    FujiValidationError,
    ParquetSink,
    ProtocolKind,
    Sample,
    sample_to_row,
)
from fujilib.registry.channels import ChannelId, Gas
from fujilib.sinks import base, row_columns
from fujilib.sinks.parquet import require_pyarrow
from fujilib.version import __version__
from tests.factories import (
    frame,
    parquet_metadata,
    parquet_table,
    read_parquet,
    reading,
    timing,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from fujilib.devices.models import Scalar

pytestmark = pytest.mark.anyio

CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)


def ok() -> Sample:
    return Sample.from_frame(frame(), device="zpa", address=1, channels=CHANNELS)


def failed() -> Sample:
    return Sample.from_error(
        FujiModbusTimeoutError("no reply"),
        device="zpa",
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        timing=timing(1000.0),
        channels=CHANNELS,
    )


async def test_an_error_first_file_reads_back_typed(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "run.parquet"
    sink = ParquetSink(path, metadata={"run": "cone-42"})
    async with sink:
        await sink.write_many([failed()])
        await sink.write_many([ok(), ok()])
    assert sink.path == path
    table = parquet_table(path)
    assert table.num_rows == 3
    assert read_parquet(path) == [sample_to_row(s) for s in (failed(), ok(), ok())]
    schema = table.schema
    assert schema.field("ch3_value").type == pa.float64()
    assert schema.field("ch3_raw").type == pa.int64()
    assert schema.field("ch3_valid").type == pa.bool_()
    assert schema.field("ch3_state").type == pa.string()
    assert not schema.field("device").nullable
    assert schema.field("ch3_value").nullable
    assert schema.names == [c.name for c in row_columns(CHANNELS)]
    meta = parquet_metadata(path)
    assert meta["fujilib.version"] == __version__
    assert meta["run"] == "cone-42"
    assert sink.metadata["run"] == "cone-42"
    assert pq.ParquetFile(path).num_row_groups == 1  # three rows, one group


async def test_rows_are_gathered_into_row_groups(tmp_path: Path) -> None:
    path = tmp_path / "groups.parquet"
    async with ParquetSink(path, row_group_size=4) as sink:
        for _ in range(10):  # ten writes of one row, as pipe() makes them
            await sink.write_many([ok()])
    groups = pq.ParquetFile(path)
    sizes = [groups.metadata.row_group(i).num_rows for i in range(groups.num_row_groups)]
    assert sizes == [4, 4, 2]
    assert len(read_parquet(path)) == 10


async def test_rows_waiting_for_their_group_hold_no_arrow_memory(tmp_path: Path) -> None:
    async with ParquetSink(tmp_path / "held.parquet", channels=CHANNELS, row_group_size=4) as sink:
        before = pa.total_allocated_bytes()
        for _ in range(3):  # one row per write, as pipe() makes them at 1 Hz
            await sink.write_many([ok()])
        assert pa.total_allocated_bytes() <= before


async def test_the_footer_is_written_when_the_last_rows_cannot_be(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unconvertible(
        sample: Sample, channels: Iterable[ChannelId] | None = None
    ) -> dict[str, Scalar]:
        return {**sample_to_row(sample, channels), "ch3_value": "not a number"}

    path = tmp_path / "last.parquet"
    sink = ParquetSink(path, row_group_size=2)
    await sink.open()
    await sink.write_many([ok(), ok()])
    monkeypatch.setattr(base, "sample_to_row", unconvertible)
    await sink.write_many([ok()])  # waits for its group
    with pytest.raises(FujiSinkWriteError, match="cannot finish"):
        await sink.close()
    assert read_parquet(path) == [sample_to_row(ok())] * 2  # the complete group


async def test_a_large_write_is_split_into_row_groups(tmp_path: Path) -> None:
    path = tmp_path / "split.parquet"
    async with ParquetSink(path, row_group_size=2) as sink:
        await sink.write_many([ok()] * 5)
    groups = pq.ParquetFile(path)
    sizes = [groups.metadata.row_group(i).num_rows for i in range(groups.num_row_groups)]
    assert sizes == [2, 2, 1]


async def test_with_channels_an_empty_recording_leaves_a_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    async with ParquetSink(path, channels=CHANNELS):
        pass
    table = parquet_table(path)
    assert table.num_rows == 0
    assert table.schema.names == [c.name for c in row_columns(CHANNELS)]


async def test_without_channels_or_rows_there_is_no_file(tmp_path: Path) -> None:
    path = tmp_path / "none.parquet"
    async with ParquetSink(path):
        pass
    assert not path.exists()


@pytest.mark.parametrize("compression", ["zstd", "snappy", "gzip", "none"])
async def test_compressions(tmp_path: Path, compression: str) -> None:
    path = tmp_path / "c.parquet"
    async with ParquetSink(path, compression=compression, row_group_size=1) as sink:  # type: ignore[arg-type]
        await sink.write_many([ok(), ok()])
    assert pq.ParquetFile(path).num_row_groups == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"compression": "lzma"},
        {"row_group_size": 0},
        {"row_group_size": True},
        {"row_group_size": None},
        {"metadata": {"a": 1}},
    ],
)
def test_bad_arguments(tmp_path: Path, kwargs: dict[str, object]) -> None:
    with pytest.raises(FujiValidationError):
        _ = ParquetSink(tmp_path / "x.parquet", **kwargs)  # type: ignore[arg-type]


async def test_a_missing_pyarrow_is_reported_at_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = ParquetSink(tmp_path / "x.parquet")  # constructing needs no pyarrow
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    with pytest.raises(FujiSinkDependencyError, match=r"fujilib\[parquet\]") as info:
        await sink.open()
    assert isinstance(info.value, FujiConfigurationError)
    with pytest.raises(FujiSinkDependencyError):
        require_pyarrow()


async def test_a_file_that_cannot_be_written(tmp_path: Path) -> None:
    with pytest.raises(FujiSinkWriteError, match="cannot open"):
        async with ParquetSink(tmp_path, channels=CHANNELS):
            pass
    sink = ParquetSink(tmp_path)
    await sink.open()
    with pytest.raises(FujiSinkWriteError, match="cannot write to"):
        await sink.write_many([ok()])
    await sink.close()


_values = st.lists(st.tuples(st.integers(-9999, 9999), st.booleans()), min_size=1, max_size=6)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(values=_values)
def test_parquet_reads_back_exactly(tmp_path: Path, values: list[tuple[int, bool]]) -> None:
    samples = [
        Sample.from_frame(
            frame((reading(ChannelId.CH3, Gas.O2, raw, 2),)),
            device="zpa",
            address=1,
            channels=(ChannelId.CH3,),
        )
        if good
        else Sample.from_error(
            FujiModbusTimeoutError("no reply"),
            device="zpa",
            address=1,
            protocol=ProtocolKind.MODBUS_RTU,
            timing=timing(),
            channels=(ChannelId.CH3,),
        )
        for raw, good in values
    ]
    path = tmp_path / "round.parquet"

    async def write() -> None:
        async with ParquetSink(path) as sink:
            await sink.write_many(samples)

    anyio.run(write)
    assert read_parquet(path) == [sample_to_row(s) for s in samples]
