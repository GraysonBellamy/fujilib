"""Unified device-library API conformance of Sample, ErrorContext, snapshots and to_pint.

The contract is recovered from the siblings' own ``test_unified_api.py`` files
(``sartoriuslib``, ``watlowlib``, ``nidaqlib``) (design §7.8); this mirrors their
assertions for these types.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

import fujilib
from fujilib import (
    Capability,
    DeviceSnapshot,
    FujiDeviceSnapshot,
    ProtocolKind,
    Sample,
    Unit,
    sample_to_row,
    to_pint,
)
from fujilib.errors import ErrorContext
from fujilib.registry.channels import ChannelId
from fujilib.units import to_pint as units_to_pint

#: The §6 top-level names these types provide.
TOP_LEVEL = ("sample_to_row", "DeviceSnapshot", "FujiDeviceSnapshot", "to_pint")


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_top_level_exports(name: str) -> None:
    assert hasattr(fujilib, name)
    assert name in fujilib.__all__


# --- §C: Sample timestamps ----------------------------------------------------------------


def test_sample_timestamp_fields_round_trip() -> None:
    now = datetime.now(UTC)
    requested = now - timedelta(milliseconds=2)
    sample = Sample(
        device="fuji",
        address=1,
        frame=None,
        protocol=ProtocolKind.MODBUS_RTU,
        t_mono_ns=12345,
        t_utc=now,
        t_midpoint_mono_ns=None,
        requested_at=requested,
        received_at=now,
        latency_s=0.001,
    )
    assert sample.t_mono_ns == 12345
    assert sample.t_utc is now
    assert sample.t_midpoint_mono_ns is None
    assert sample.requested_at is requested
    assert sample.received_at is now
    assert sample.latency_s == 0.001
    assert dict(sample.metadata) == {}
    assert sample.error is None


def test_sample_fields() -> None:
    names = [f.name for f in dataclasses.fields(Sample)]
    for required in (
        "t_mono_ns",
        "t_utc",
        "t_midpoint_mono_ns",
        "requested_at",
        "received_at",
        "latency_s",
        "metadata",
        "error",
    ):
        assert required in names


# --- §G: ErrorContext.address -----------------------------------------------------------------


def test_error_context_address() -> None:
    assert ErrorContext().address is None
    assert ErrorContext(address=9).address == 9


# --- §H: snapshots ------------------------------------------------------------------------


def test_device_snapshot_base_fields() -> None:
    assert [f.name for f in dataclasses.fields(DeviceSnapshot)] == [
        "name",
        "model",
        "firmware",
        "serial",
        "connected",
        "last_error",
        "recoverable_error_count",
        "captured_at",
    ]


def test_fuji_snapshot_extends_the_base() -> None:
    snap = FujiDeviceSnapshot(
        name="fuji",
        model="ZPA",
        firmware=None,
        serial="N8A0259T",
        connected=True,
        last_error=None,
        recoverable_error_count=0,
        captured_at=datetime.now(UTC),
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        type_code="ZPACBJY1MPFYYYYYY2DEYAYAY0",
        capabilities=Capability.CLOCK | Capability.ADC_VALUES,
        availability={},
        channels=(ChannelId.CH1, ChannelId.CH2, ChannelId.CH3),
    )
    assert isinstance(snap, DeviceSnapshot)
    assert snap.firmware is None  # the program version is not readable
    assert snap.recoverable_error_count == 0
    assert snap.captured_at.tzinfo is not None


# --- §K: to_pint --------------------------------------------------------------------------


def test_to_pint_is_exported_from_both_places() -> None:
    assert to_pint is units_to_pint


def test_to_pint_contract() -> None:
    for unit in Unit:
        result = to_pint(unit)
        assert result is None or isinstance(result, str)
    assert to_pint("ppm") == to_pint(Unit.PPM) == "ppm"
    assert to_pint("no-such-unit") is None
    assert to_pint(None) is None
    # vol% and ppm differ by 10^4; their strings must differ too.
    assert to_pint(Unit.VOL_PERCENT) != to_pint(Unit.PPM)


def test_sample_to_row_is_the_top_level_flattener() -> None:
    assert fujilib.sample_to_row is sample_to_row
