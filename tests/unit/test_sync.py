"""The blocking facade: the portal, ``Fuji.open`` and discovery (design §7.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from fujilib import (
    Availability,
    Capability,
    FujiConnectionError,
    FujiFirmwareError,
    Gas,
    ProtocolKind,
    ReadingState,
)
from fujilib.sync import Fuji, SyncAnalyzer, SyncPortal, find_devices
from fujilib.testing import mock_transport
from fujilib.transport.serial import SerialTransport
from tests.facade import bench

if TYPE_CHECKING:
    from fujilib.transport.base import SerialSettings


# --- The portal ------------------------------------------------------------------------------


async def fail(exc: BaseException) -> None:
    raise exc


def running(portal: SyncPortal) -> bool:
    return portal.running


def test_the_portal_is_used_once() -> None:
    portal = SyncPortal()
    assert not running(portal)
    with pytest.raises(RuntimeError, match="not running"):
        portal.call(fail, ValueError("x"))
    with portal:
        assert running(portal)
    assert not running(portal)
    portal.__exit__(None, None, None)  # a second exit is harmless
    with pytest.raises(RuntimeError, match="cannot be used again"), portal:
        pass
    with pytest.raises(RuntimeError, match="not running"):
        portal.wrap_async_context_manager(mock_transport())


def test_a_group_of_one_is_unwrapped() -> None:
    with SyncPortal() as portal:
        with pytest.raises(ValueError, match="alone"):
            portal.call(fail, ExceptionGroup("g", [ExceptionGroup("h", [ValueError("alone")])]))
        with pytest.raises(ExceptionGroup) as caught:
            portal.call(fail, ExceptionGroup("g", [ValueError("a"), KeyError("b")]))
        assert len(caught.value.exceptions) == 2
        with pytest.raises(KeyError):
            portal.call(fail, KeyError("plain"))
        caused = ValueError("inner")
        caused.__cause__ = OSError("the original")
        with pytest.raises(ValueError, match="inner") as unwrapped:
            portal.call(fail, ExceptionGroup("g", [caused]))
        assert isinstance(unwrapped.value.__cause__, OSError)


# --- The analyzer ----------------------------------------------------------------------------


def test_every_method_on_the_simulator() -> None:
    mock = bench()
    with (
        SyncPortal() as portal,
        portal.wrap_async_context_manager(mock_transport(mock)) as (transport, _line),
        Fuji.open(
            transport, channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}, portal=portal
        ) as anz,
    ):
        assert isinstance(anz, SyncAnalyzer)
        assert anz.portal is portal
        assert anz.analyzer.session is anz.session
        assert anz.info is not None
        assert [c.gas for c in anz.channels] == [Gas.CO2, Gas.CO, Gas.O2]
        assert (anz.address, anz.port, anz.protocol) == (1, "mock://zp", ProtocolKind.MODBUS_RTU)
        assert anz.identify(channel_map={"CH3": "o2"}).model == "ZPA"
        assert len(anz.read_ranges()) == 5
        assert anz.read_metadata().response_time_o2_s == 15
        frame = anz.poll()
        assert anz.last_frame is frame
        assert anz.poll(detail=False).analyzer is None
        assert anz.read_channel("CH3").state is ReadingState.OK
        assert anz.status().instrument_error is False
        assert anz.channel_status("CH1").range == 1
        assert anz.read_clock().clock.year == 2026
        assert len(anz.read_adc().raw) == 21
        assert anz.reprobe(Capability.CLOCK) is Availability.SUPPORTED
        assert anz.read_error_log()
        with pytest.raises(FujiFirmwareError):
            anz.read_calibration_log()
        assert anz.read_parameter("response_time.o2").value == 15
        assert list(anz.read_parameters(["response_time.o2"])) == ["response_time.o2"]
        assert "hold.mode" in anz.read_settings()
        assert anz.snapshot(name="zpa").name == "zpa"
        assert repr(anz) == "<SyncAnalyzer ZPA on mock://zp station 1>"


def test_the_analyzer_closes_with_its_block() -> None:
    mock = bench()
    with (
        SyncPortal() as portal,
        portal.wrap_async_context_manager(mock_transport(mock)) as (transport, _line),
    ):
        with Fuji.open(transport, identify=False, portal=portal) as anz:
            pass
        with pytest.raises(FujiConnectionError, match="closed"):
            anz.poll()
        assert transport.is_open  # the caller's transport stays open
        with Fuji.open(transport, identify=False, portal=portal) as again, again:
            pass


def test_fuji_open_with_a_portal_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_port(settings: SerialSettings) -> SerialTransport:
        config = SerialConfig(baudrate=settings.baudrate)
        host, device = serial_port_pair(config_a=config, config_b=config)
        await device.aclose()
        return SerialTransport(host, settings)

    monkeypatch.setattr(SerialTransport, "open", open_port)
    with Fuji.open("COM8", identify=False) as anz:
        assert anz.snapshot().connected
        assert anz.portal.running
    assert not anz.portal.running


# --- Discovery -------------------------------------------------------------------------------


@pytest.mark.parametrize("shared", [False, True])
def test_find_devices(monkeypatch: pytest.MonkeyPatch, shared: bool) -> None:
    async def unopenable(settings: SerialSettings) -> Any:
        msg = f"cannot open {settings.port}"
        raise FujiConnectionError(msg)

    monkeypatch.setattr(SerialTransport, "open", unopenable)
    if shared:
        with SyncPortal() as portal:
            results = find_devices(ports=["COM9"], addresses=(1, 2), portal=portal)
    else:
        results = find_devices(ports=["COM9"])
    assert [r.ok for r in results] == [False] * len(results)
    assert all(isinstance(r.error, FujiConnectionError) for r in results)
