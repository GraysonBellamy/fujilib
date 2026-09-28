"""``open_device``: arguments, ownership and cleanup (design §7.1, unified API §A)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from fujilib import open_device
from fujilib.errors import (
    FujiConfigurationError,
    FujiConnectionError,
    FujiProtocolUnsupportedError,
    FujiValidationError,
)
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.registry.channels import ChannelId, Gas, LabelSource
from fujilib.testing import FaultKind, mock_transport
from fujilib.transport.base import SerialSettings
from fujilib.transport.serial import SerialTransport
from tests.facade import IDENTIFY, PROBES, bench

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.anyio


class Opener:
    """Stands in for ``SerialTransport.open``, handing out one simulated port."""

    def __init__(self, transport: SerialTransport) -> None:
        self.transport = transport
        self.opened: list[SerialSettings] = []

    async def __call__(self, settings: SerialSettings) -> SerialTransport:
        self.opened.append(settings)
        return self.transport


@pytest.fixture
async def opener(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[tuple[Opener, Any]]:
    mock = bench()
    async with mock_transport(mock) as (transport, _line):
        stand_in = Opener(transport)
        monkeypatch.setattr(SerialTransport, "open", stand_in)
        yield stand_in, mock


async def test_open_a_port_by_name(opener: tuple[Opener, Any]) -> None:
    stand_in, mock = opener
    async with await open_device("COM8", channel_map={"CH3": "o2"}) as anz:
        info = anz.info
        assert stand_in.transport.is_open
    assert stand_in.opened == [SerialSettings(port="COM8")]
    assert mock.transactions() == IDENTIFY + PROBES
    assert info is not None
    assert info.channels[2].gas is Gas.O2
    assert info.channels[2].label_source is LabelSource.ASSERTED
    assert not stand_in.transport.is_open  # opened by name, so closed with the analyzer


async def test_the_channel_map_takes_members_or_names(opener: tuple[Opener, Any]) -> None:
    _stand_in, _mock = opener
    anz = await open_device("COM8", identify=False, channel_map={ChannelId.CH3: Gas.O2, "1": "CO2"})
    await anz.close()
    assert dict(anz.session.asserted) == {ChannelId.CH1: Gas.CO2, ChannelId.CH3: Gas.O2}


async def test_open_without_identifying(opener: tuple[Opener, Any]) -> None:
    _stand_in, mock = opener
    anz = await open_device("COM8", identify=False)
    try:
        assert anz.info is None
        assert anz.channels == ()
    finally:
        await anz.close()
    assert mock.transactions() == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (SerialSettings(port=""), SerialSettings(port="COM8")),
        (
            SerialSettings(port="COM8", baudrate=19_200),
            SerialSettings(port="COM8", baudrate=19_200),
        ),
    ],
)
async def test_serial_settings(
    opener: tuple[Opener, Any], given: SerialSettings, expected: SerialSettings
) -> None:
    stand_in, _mock = opener
    anz = await open_device("COM8", serial_settings=given, identify=False)
    await anz.close()
    assert stand_in.opened == [expected]


async def test_a_failed_identify_closes_the_port_it_opened(opener: tuple[Opener, Any]) -> None:
    stand_in, mock = opener
    mock.set_register("identity.type_code", encode_chars("XYZ", 26))
    with pytest.raises(FujiProtocolUnsupportedError):
        await open_device("COM8")
    assert not stand_in.transport.is_open


async def test_a_cancelled_open_closes_the_port_it_opened(opener: tuple[Opener, Any]) -> None:
    stand_in, mock = opener
    mock.inject(FaultKind.DELAY, delay_s=0.2)
    with anyio.move_on_after(0.05):
        await open_device("COM8")
    assert not stand_in.transport.is_open


async def test_a_callers_transport_stays_open() -> None:
    mock = bench()
    async with mock_transport(mock) as (transport, _line):
        mock.set_register("identity.type_code", encode_chars("XYZ", 26))
        with pytest.raises(FujiProtocolUnsupportedError):
            await open_device(transport)
        assert transport.is_open
        mock.set_register("identity.type_code", encode_chars("ZPACBJY1MPFYYYYYY2DEYAYAY0", 26))
        # The failed open released its claim on the transport, so it can be opened again.
        anz = await open_device(transport, address=1)
        await anz.close()
        assert transport.is_open


async def test_one_analyzer_per_transport() -> None:
    async with mock_transport(bench()) as (transport, _line):
        first = await open_device(transport, identify=False)
        try:
            with pytest.raises(FujiConfigurationError, match="already has an open Modbus port"):
                await open_device(transport, identify=False)
        finally:
            await first.close()


async def test_a_closed_transport_is_refused() -> None:
    async with mock_transport(bench()) as (transport, _line):
        await transport.aclose()
        with pytest.raises(FujiConnectionError, match="closed"):
            await open_device(transport)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"protocol": "modbus_ascii"}, "speak only modbus_rtu"),
        ({"address": 0}, "station number 1-31"),
        ({"address": 32}, "station number 1-31"),
        ({"address": True}, "must be an integer"),
        ({"channel_map": {"CH13": "o2"}}, "unknown channel"),
        ({"channel_map": {"CH3": "argon"}}, "unknown gas"),
        ({"channel_map": {"CH3": "unknown"}}, "cannot be asserted as unknown"),
        ({"channel_map": {"ch3": "o2", "CH3": "co"}}, "asserted as both"),
        ({"serial_settings": SerialSettings(port="COM9")}, "serial_settings name port"),
    ],
)
async def test_invalid_arguments_open_nothing(
    opener: tuple[Opener, Any], kwargs: dict[str, Any], match: str
) -> None:
    stand_in, _mock = opener
    with pytest.raises(FujiValidationError, match=match):
        await open_device("COM8", **kwargs)
    assert stand_in.opened == []


async def test_the_protocol_may_be_named(opener: tuple[Opener, Any]) -> None:
    _stand_in, _mock = opener
    anz = await open_device("COM8", protocol="modbus_rtu", identify=False)
    await anz.close()


async def test_settings_do_not_apply_to_an_open_transport() -> None:
    async with mock_transport(bench()) as (transport, _line):
        with pytest.raises(FujiValidationError, match="open transport"):
            await open_device(transport, serial_settings=SerialSettings(port="x"))
        with pytest.raises(FujiValidationError, match="port name or an open Transport"):
            await open_device(42)  # type: ignore[arg-type]


async def test_an_invalid_request_timeout_closes_the_port(opener: tuple[Opener, Any]) -> None:
    stand_in, _mock = opener
    with pytest.raises(FujiValidationError, match="request_timeout"):
        await open_device("COM8", timeout=0)
    assert not stand_in.transport.is_open


async def test_a_port_that_does_not_exist() -> None:
    name = "COM249" if sys.platform == "win32" else "/dev/fujilib-no-such-port"
    with pytest.raises(FujiConnectionError):
        await open_device(name)
