"""Transports: canonical port names, the serial transport and the scripted fake (design §4.1)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import anyio
import anyserial
import pytest
from anyserial import FlowControl, Parity, SerialConfig, SerialPort, StopBits
from anyserial.testing import serial_port_pair

from fujilib.errors import FujiConfigurationError, FujiConnectionError, FujiValidationError
from fujilib.transport import serial as serial_module
from fujilib.transport.base import FUJI_BAUDRATE, SerialSettings, Transport
from fujilib.transport.fake import FakeTransport
from fujilib.transport.serial import SerialTransport, serial_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

BACKSLASH = "\\"


# --- Canonical names ------------------------------------------------------------------


def record_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make ``open_serial_port`` record each path it is given, then find no port there."""
    opened: list[str] = []

    async def fake_open(path: str, config: SerialConfig) -> SerialPort:
        opened.append(path)
        raise anyserial.PortNotFoundError(path)

    monkeypatch.setattr(serial_module, "open_serial_port", fake_open)
    return opened


async def opened_as(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    """The name under which ``SerialTransport.open`` opens port ``name``."""
    opened = record_opens(monkeypatch)
    with pytest.raises(FujiConnectionError) as info:
        await SerialTransport.open(SerialSettings(port=name))
    (path,) = opened
    assert info.value.context.port == path
    return path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows device names")
@pytest.mark.parametrize(
    ("name", "canonical"),
    [
        ("COM8", "COM8"),
        ("com8", "COM8"),
        (" COM8 ", "COM8"),
        (BACKSLASH * 2 + "." + BACKSLASH + "COM8", "COM8"),
        (BACKSLASH * 2 + "?" + BACKSLASH + "com8", "COM8"),
        (BACKSLASH * 2 + "." + BACKSLASH + "com10", "COM10"),
    ],
)
async def test_windows_spellings_of_one_port_open_it_by_one_name(
    monkeypatch: pytest.MonkeyPatch, name: str, canonical: str
) -> None:
    assert await opened_as(monkeypatch, name) == canonical


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX device paths")
async def test_a_posix_symlink_opens_its_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "ttyUSB0"
    target.touch()
    link = tmp_path / "usb-FTDI-if00-port0"
    link.symlink_to(target)
    assert await opened_as(monkeypatch, f" {link} ") == str(target.resolve())
    assert await opened_as(monkeypatch, "/dev/fujilib-absent") == "/dev/fujilib-absent"


@pytest.mark.parametrize("name", ["", "   "])
async def test_an_empty_name_is_refused_before_opening(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    opened = record_opens(monkeypatch)
    with pytest.raises(FujiValidationError, match="empty"):
        await SerialTransport.open(SerialSettings(port=name))
    assert opened == []


# --- Serial transport -----------------------------------------------------------------


def is_open(transport: Transport) -> bool:
    return transport.is_open


def test_serial_config_maps_every_setting() -> None:
    settings = SerialSettings(
        port="COM8",
        parity=anyserial.Parity.EVEN,
        stopbits=StopBits.TWO,
        rtscts=True,
        xonxoff=True,
        exclusive=False,
    )
    assert serial_config(settings) == SerialConfig(
        baudrate=FUJI_BAUDRATE,
        parity=Parity.EVEN,
        stop_bits=StopBits.TWO,
        flow_control=FlowControl(xon_xoff=True, rts_cts=True),
        exclusive=False,
    )


def test_the_default_settings_are_38400_8n1() -> None:
    config = serial_config(SerialSettings(port="COM8"))
    assert (config.baudrate, config.byte_size, config.parity, config.stop_bits) == (
        38_400,
        anyserial.ByteSize.EIGHT,
        Parity.NONE,
        StopBits.ONE,
    )
    assert config.exclusive


async def test_open_uses_the_canonical_name(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    host, device = serial_port_pair()

    async def fake_open(path: str, config: SerialConfig) -> SerialPort:
        opened.append(path)
        assert config.baudrate == FUJI_BAUDRATE
        return host

    monkeypatch.setattr(serial_module, "open_serial_port", fake_open)
    name = "com8" if sys.platform == "win32" else "/dev/fujilib-absent"
    async with await SerialTransport.open(SerialSettings(port=name)) as transport:
        assert opened == [anyserial.canonical_port_name(name)]
        assert transport.label == anyserial.canonical_port_name(name)
        assert transport.settings.port == transport.label
        assert transport.stream is host
        assert is_open(transport)
        assert isinstance(transport, Transport)
        assert "open" in repr(transport)
    assert not is_open(transport)
    assert "closed" in repr(transport)
    await transport.aclose()  # idempotent
    await device.aclose()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (anyserial.PortNotFoundError("no such port"), FujiConnectionError),
        (anyserial.PortBusyError("in use"), FujiConnectionError),
        (anyserial.ConfigurationError("bad baud"), FujiConfigurationError),
    ],
)
async def test_open_failures_are_mapped(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[Exception]
) -> None:
    async def fail(path: str, config: SerialConfig) -> SerialPort:
        raise error

    monkeypatch.setattr(serial_module, "open_serial_port", fail)
    with pytest.raises(expected) as info:
        await SerialTransport.open(SerialSettings(port="COM8"))
    assert info.value.__cause__ is error
    assert info.value.context.port == "COM8"  # type: ignore[attr-defined]


async def test_opening_a_port_that_does_not_exist_fails_cleanly() -> None:
    name = "COM249" if sys.platform == "win32" else "/dev/fujilib-no-such-port"
    with pytest.raises(FujiConnectionError, match="cannot open"):
        await SerialTransport.open(SerialSettings(port=name))


async def test_settings_anyserial_rejects_are_a_configuration_error() -> None:
    with pytest.raises(FujiConfigurationError, match="settings"):
        await SerialTransport.open(SerialSettings(port="COM8", baudrate=0))


# --- Fake transport -------------------------------------------------------------------


async def test_fake_replays_scripted_replies() -> None:
    fake = FakeTransport({b"\x01\x02": b"\xaa\xbb\xcc"}, label="fake://test")
    assert isinstance(fake, Transport)
    assert fake.stream is fake
    assert fake.label == fake.settings.port == "fake://test"
    await fake.send(b"\x01\x02")
    assert await fake.receive(2) == b"\xaa\xbb"
    assert await fake.receive() == b"\xcc"
    assert fake.writes == [b"\x01\x02"]
    assert fake.unmatched == []
    await fake.send_eof()


async def test_fake_records_requests_it_has_no_reply_for() -> None:
    fake = FakeTransport()
    await fake.send(b"\x09")
    assert fake.unmatched == [b"\x09"]
    with anyio.move_on_after(0.01) as scope:
        await fake.receive()
    assert scope.cancelled_caught


async def test_a_pending_receive_waits_for_the_reply() -> None:
    fake = FakeTransport({b"q": b"answer"})
    received: list[bytes] = []

    async def reader() -> None:
        received.append(await fake.receive())

    async with anyio.create_task_group() as tg:
        _ = tg.start_soon(reader)
        await anyio.sleep(0.01)
        await fake.send(b"q")
    assert received == [b"answer"]


async def test_closing_the_fake_wakes_a_pending_receive() -> None:
    fake = FakeTransport()
    errors: list[BaseException] = []

    async def reader() -> None:
        try:
            await fake.receive()
        except anyio.ClosedResourceError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        _ = tg.start_soon(reader)
        await anyio.sleep(0.01)
        await fake.aclose()
    assert len(errors) == 1
    assert not fake.is_open
    with pytest.raises(anyio.ClosedResourceError):
        await fake.send(b"x")
    await fake.aclose()
