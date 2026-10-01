"""The sample shape capa consumes: Frame -> Sample -> row -> capa records (design §7.6, §7.8).

capa's adapter for fujilib will follow its alicat adapter: one ``wide_row``
``SourceRecord`` per tick built from ``sample_to_row()``, and one
``ChannelSample`` per bound channel whose ``source_field`` is a row column.
A channel binds a reading's value or its validity (1.0 or 0.0), since capa
stores a sample's status but reads it nowhere (design §13.1 #106). Error
samples produce the record but no channel samples (capa's sartorius path).
This test builds those records from fujilib's own types, so the row
shape, units, timestamps, validity columns, error rows and snapshot shape are
fixed before any I/O layer exists.

The field lists mirror ``capa/src/capa/devices/records.py``. When capa is
installed in the environment, the last test also builds capa's real models.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from fujilib import (
    DeviceInfo,
    DeviceSnapshot,
    Gas,
    LabelSource,
    ReadingState,
    Sample,
    sample_to_row,
    to_pint,
)
from fujilib.errors import FujiModbusTimeoutError
from fujilib.protocol.base import ProtocolKind
from fujilib.registry.channels import ChannelId
from fujilib.sinks.base import row_columns
from tests.factories import MONO0, analyzer, frame, reading, status, timing

if TYPE_CHECKING:
    from collections.abc import Mapping

#: capa's ``SourceRecord`` fields.
SOURCE_RECORD_FIELDS = (
    "record_id",
    "adapter",
    "device",
    "shape",
    "t_mono_ns",
    "t_utc",
    "row",
    "block_ref",
    "metadata",
)
#: capa's ``ChannelSample`` fields.
CHANNEL_SAMPLE_FIELDS = (
    "channel",
    "t_mono_ns",
    "t_mono_s",
    "value",
    "raw",
    "unit",
    "uncertainty",
    "status",
    "source_record_id",
    "source_field",
    "metadata",
)
#: Unit strings capa's pint registry parses and canonicalizes.
CAPA_UNITS = {"percent", "ppm", "mg/m**3", "g/m**3"}
#: capa's ``AnalyzerCalibration.analyzer`` values.
CAPA_ANALYZERS = {"o2", "co", "co2"}
#: Row keys capa's device-record sink reserves.
RESERVED = {"record_id"}
#: The declared channels of a cone-calorimeter run.
BINDINGS = {"co2": ChannelId.CH1, "co": ChannelId.CH2, "o2": ChannelId.CH3}
CHANNELS = tuple(BINDINGS.values())
RUN_START = MONO0 - 1_000_000_000


def to_source_record(sample: Sample, *, seq: int) -> dict[str, Any]:
    """What capa's adapter would build, following its alicat ``_record_for``."""
    return {
        "record_id": f"fuji:{sample.device}:{seq}",
        "adapter": "fuji",
        "device": sample.device,
        "shape": "wide_row",
        "t_mono_ns": sample.t_mono_ns - RUN_START,
        "t_utc": sample.t_utc,
        "row": sample_to_row(sample, CHANNELS),
        "block_ref": None,
        "metadata": {"address": sample.address},
    }


def to_channel_samples(sample: Sample, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What capa's adapter would build per bound channel."""
    if sample.frame is None:
        return []  # an error sample yields the record only
    out: list[dict[str, Any]] = []
    t_mono_ns = record["t_mono_ns"]
    for name, channel in BINDINGS.items():
        reading = sample.frame.channel(channel)
        if reading.value is None:
            continue
        unit = to_pint(reading.unit)
        assert unit is not None
        out.append(
            {
                "channel": name,
                "t_mono_ns": t_mono_ns,
                "t_mono_s": t_mono_ns / 1e9,
                "value": reading.value,
                "raw": reading.raw_value,
                "unit": unit,
                "uncertainty": None,
                "status": reading.state.value,
                "source_record_id": record["record_id"],
                "source_field": f"ch{channel.number}_value",
                "metadata": {},
            }
        )
    return out


def to_validity_samples(sample: Sample, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What capa's adapter would build per channel bound to a reading's validity."""
    if sample.frame is None:
        return []
    out: list[dict[str, Any]] = []
    t_mono_ns = record["t_mono_ns"]
    for name, channel in BINDINGS.items():
        reading = sample.frame.channel(channel)
        if reading.valid is None:
            continue  # unknown validity is not reported as either
        out.append(
            {
                "channel": f"{name}_valid",
                "t_mono_ns": t_mono_ns,
                "t_mono_s": t_mono_ns / 1e9,
                "value": 1.0 if reading.valid else 0.0,
                "raw": reading.valid,
                "unit": "dimensionless",
                "uncertainty": None,
                "status": reading.state.value,
                "source_record_id": record["record_id"],
                "source_field": f"ch{channel.number}_valid",
                "metadata": {},
            }
        )
    return out


def ok_sample() -> Sample:
    held_o2 = reading(
        ChannelId.CH3, Gas.O2, 2029, 2, channel_status=status(hold=True), state=ReadingState.HOLD
    )
    readings = (
        reading(ChannelId.CH1, Gas.CO2, -11, 2),
        reading(ChannelId.CH2, Gas.CO, -9, 3),
        held_o2,
    )
    return Sample.from_frame(frame(readings), device="zpa", address=1)


def error_sample() -> Sample:
    return Sample.from_error(
        FujiModbusTimeoutError("no reply"),
        device="zpa",
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        timing=timing(1000.0),
    )


# --- SourceRecord -----------------------------------------------------------------------


def test_source_record_shape() -> None:
    record = to_source_record(ok_sample(), seq=0)
    assert tuple(record) == SOURCE_RECORD_FIELDS
    assert record["t_mono_ns"] == 1_010_000_000  # run-relative, from the monotonic clock
    assert record["t_utc"].tzinfo is not None
    for key, value in record["row"].items():
        assert value is None or type(value) in {float, int, str, bool}, key
    assert not RESERVED & record["row"].keys()


def test_error_records_keep_the_same_row_keys() -> None:
    ok = to_source_record(ok_sample(), seq=0)
    err = to_source_record(error_sample(), seq=1)
    assert list(ok["row"]) == list(err["row"])
    assert err["row"]["error_type"] == "fujilib.errors.FujiModbusTimeoutError"
    assert to_channel_samples(error_sample(), err) == []


def test_error_first_recording_keeps_typed_columns() -> None:
    # A sink fixes its schema from row_columns(), not from the first row, so an
    # error-first recording still types ch3_value as float.
    specs = {s.name: s for s in row_columns(CHANNELS)}
    assert specs["ch3_value"].python_type is float
    assert specs["ch3_raw"].python_type is int
    assert specs["ch3_valid"].python_type is bool
    assert specs["ch3_state"].python_type is str
    assert set(specs) == set(sample_to_row(error_sample(), CHANNELS))


# --- ChannelSample -------------------------------------------------------------------------


def test_channel_samples() -> None:
    sample = ok_sample()
    record = to_source_record(sample, seq=0)
    samples = {s["channel"]: s for s in to_channel_samples(sample, record)}
    assert set(samples) == set(BINDINGS)
    for name, cs in samples.items():
        assert tuple(cs) == CHANNEL_SAMPLE_FIELDS
        assert isinstance(cs["value"], float)
        assert cs["unit"] in CAPA_UNITS
        assert cs["source_field"] in record["row"]
        assert record["row"][cs["source_field"]] == cs["value"]
        assert cs["status"] in {s.value for s in ReadingState}
        assert name in CAPA_ANALYZERS
    assert samples["o2"]["status"] == "hold"
    assert samples["co2"]["status"] == "ok"
    assert samples["o2"]["unit"] == "percent"
    assert record["row"]["ch3_valid"] is False
    assert record["row"]["ch3_state"] == "hold"


def test_an_undecodable_value_yields_no_channel_sample() -> None:
    sample = ok_sample()
    assert sample.frame is not None
    broken = dataclasses.replace(sample.frame.readings[0], value=None, state=ReadingState.UNKNOWN)
    f = dataclasses.replace(sample.frame, readings=(broken, *sample.frame.readings[1:]))
    sample = Sample.from_frame(f, device="zpa", address=1)
    names = {s["channel"] for s in to_channel_samples(sample, to_source_record(sample, seq=0))}
    assert names == {"co", "o2"}


def test_validity_samples() -> None:
    sample = ok_sample()
    record = to_source_record(sample, seq=0)
    samples = {s["channel"]: s for s in to_validity_samples(sample, record)}
    assert set(samples) == {f"{name}_valid" for name in BINDINGS}
    for cs in samples.values():
        assert tuple(cs) == CHANNEL_SAMPLE_FIELDS
        assert cs["value"] in {0.0, 1.0}
        assert record["row"][cs["source_field"]] is cs["raw"]
    assert samples["co2_valid"]["value"] == 1.0
    assert (samples["o2_valid"]["value"], samples["o2_valid"]["status"]) == (0.0, "hold")


def test_unknown_validity_yields_no_validity_sample() -> None:
    # Without the status block every state is unknown: neither valid nor invalid.
    unknown = tuple(
        dataclasses.replace(r, state=ReadingState.UNKNOWN, status=None)
        for r in frame(detail=False).readings
    )
    sample = Sample.from_frame(frame(unknown, detail=False), device="zpa", address=1)
    record = to_source_record(sample, seq=0)
    assert to_validity_samples(sample, record) == []
    assert record["row"]["ch3_valid"] is None
    assert len(to_channel_samples(sample, record)) == len(BINDINGS)  # the values still flow


def test_a_settling_reading_is_a_sample_that_says_so() -> None:
    # After a reconnect the value is kept and the state says not to trust it; capa's
    # balance adapter uses the same word for an unstable reading.
    settling = tuple(dataclasses.replace(r, state=ReadingState.SETTLING) for r in frame().readings)
    sample = Sample.from_frame(frame(settling), device="zpa", address=1)
    record = to_source_record(sample, seq=0)
    values = to_channel_samples(sample, record)
    assert {cs["status"] for cs in values} == {"settling"}
    assert {cs["value"] for cs in to_validity_samples(sample, record)} == {0.0}
    assert record["row"]["ch3_state"] == "settling"
    assert record["row"]["ch3_valid"] is False


def test_expected_gas_check_has_typed_labels() -> None:
    # capa checks an operator-declared gas against the reading, as its watlow
    # adapter checks the wire unit. It needs typed gas, label source and unit.
    sample = ok_sample()
    assert sample.frame is not None
    for name, channel in BINDINGS.items():
        reading = sample.frame.channel(channel)
        assert reading.gas.value == name
        assert reading.label_source is LabelSource.ASSERTED
    assert to_pint(sample.frame.channel("CH1").unit) != to_pint("ppm")


def test_analyzer_status_travels_once_per_tick() -> None:
    sample = Sample.from_frame(
        frame(status_block=analyzer(instrument_error=True)), device="zpa", address=1
    )
    row = sample_to_row(sample, CHANNELS)
    assert row["instrument_error"] is True
    assert sum(1 for k in row if k == "instrument_error") == 1


# --- Snapshot and identity -------------------------------------------------------------------


def test_snapshot_and_identity_fields_capa_reads() -> None:
    assert "recoverable_error_count" in {f.name for f in dataclasses.fields(DeviceSnapshot)}
    # capa's equipment record probes model, serial_number and firmware.
    names = {f.name for f in dataclasses.fields(DeviceInfo)}
    assert {"model", "serial_number", "firmware"} <= names


# --- capa's own models, when capa is installed ---------------------------------------------------


def test_rows_build_capa_records() -> None:
    records = pytest.importorskip("capa.devices.records")
    for seq, sample in enumerate((ok_sample(), error_sample())):
        record = to_source_record(sample, seq=seq)
        source = records.SourceRecord(**record)
        for cs in (*to_channel_samples(sample, record), *to_validity_samples(sample, record)):
            records.ChannelSample(**cs)
        assert source.t_utc == datetime.fromisoformat(record["row"]["t_utc"]).astimezone(UTC)
