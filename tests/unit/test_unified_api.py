"""Unified device-library API conformance (design §7.8).

The contract is recovered from the siblings' own ``test_unified_api.py`` files
(``sartoriuslib``, ``watlowlib``, ``nidaqlib``); this mirrors their assertions:
the entry point (§A), discovery results (§B), sample timestamps (§C), poll
sources (§E), error context (§G), snapshots (§H), the recovered-error count
(§J), ``to_pint`` (§K) and the top-level names (§6).
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta

import pytest

import fujilib
from fujilib import (
    Analyzer,
    Capability,
    DeviceResult,
    DeviceSnapshot,
    DiscoveryResult,
    FujiConnectionError,
    FujiDeviceSnapshot,
    PollSourceAdapter,
    ProtocolKind,
    Sample,
    Unit,
    open_device,
    sample_to_row,
    to_pint,
)
from fujilib.errors import ErrorContext
from fujilib.registry.channels import ChannelId
from fujilib.testing import FaultKind, mock_transport
from fujilib.units import to_pint as units_to_pint
from tests.facade import analyzer_on, bench

#: The §6 top-level names.
TOP_LEVEL = (
    "open_device",
    "find_devices",
    "sample_to_row",
    "PollSourceAdapter",
    "DeviceResult",
    "DiscoveryResult",
    "DiscoverySummary",
    "DeviceSnapshot",
    "FujiDeviceSnapshot",
    "to_pint",
)


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


# --- §A: the entry point ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_open_device_is_an_async_context_manager() -> None:
    async with mock_transport(bench()) as (transport, _line):
        async with await open_device(transport) as anz:
            assert isinstance(anz, Analyzer)
            assert (await anz.snapshot()).connected
        await anz.close()  # idempotent
        with pytest.raises(FujiConnectionError):
            await anz.poll()


# --- §B: discovery results ------------------------------------------------------------------


def test_discovery_result_by_keyword() -> None:
    result = DiscoveryResult(
        ok=False,
        port="COM8",
        address=1,
        baudrate=38_400,
        protocol=None,
        device_info=None,
        error=None,
        elapsed_s=0.3,
    )
    assert result.model is None


# --- §E: poll sources ---------------------------------------------------------------------


def test_device_result_and_poll_source_adapter() -> None:
    assert DeviceResult.success(1).ok
    assert not DeviceResult[int].failure(FujiConnectionError("gone")).ok
    params = list(inspect.signature(PollSourceAdapter).parameters)
    assert params == ["name", "device"]


# --- §J: recovered errors -------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_session_counts_recovered_errors() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        assert anz.session.recoverable_error_count == 0
        mock.inject(FaultKind.CORRUPT_CRC)
        await anz.poll()
        assert anz.session.recoverable_error_count == 1
        assert (await anz.snapshot()).recoverable_error_count == 1
