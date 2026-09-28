"""Discovery: probing stations for analyzers, read-only (design §7.5, unified API §B)."""

from __future__ import annotations

import dataclasses
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from fujilib import (
    DiscoveryResult,
    DiscoverySummary,
    FujiConnectionError,
    FujiFrameError,
    FujiModbusIllegalDataAddressError,
    FujiModbusTimeoutError,
    FujiProtocolUnsupportedError,
    FujiValidationError,
    ProtocolKind,
    find_devices,
    summarize_discovery,
)
from fujilib.devices import discovery
from fujilib.devices.profile import ZP_PROFILE, recognize_zp
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockAnalyzer, MockRegion, mock_transport
from fujilib.transport.serial import SerialTransport
from tests.facade import FC04, IDENTIFY, PROBES, bench, when

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

    from fujilib.transport.base import SerialSettings

pytestmark = pytest.mark.anyio

PROBE = [(FC04, 0x0448, 3)]
QUICK: dict[str, Any] = {"per_probe_timeout_s": 0.1}


@asynccontextmanager
async def serial_ports(
    monkeypatch: pytest.MonkeyPatch,
    lines: Mapping[str, Sequence[MockAnalyzer]],
    *,
    opens: int = 1,
) -> AsyncGenerator[list[str]]:
    """Ports named after ``lines`` carrying their analyzers; any other name will not open.

    Each port can be opened ``opens`` times, each time on a fresh line.
    """
    async with AsyncExitStack() as stack:
        spare: dict[str, list[SerialTransport]] = {}
        for name, analyzers in lines.items():
            for _ in range(opens):
                transport, _line = await stack.enter_async_context(
                    mock_transport(*analyzers, label=name)
                )
                spare.setdefault(name, []).append(transport)
        opened: list[str] = []

        async def open_port(settings: SerialSettings) -> SerialTransport:
            opened.append(settings.port)
            if not spare.get(settings.port):
                msg = f"cannot open {settings.port}"
                raise FujiConnectionError(msg)
            return spare[settings.port].pop(0)

        monkeypatch.setattr(SerialTransport, "open", open_port)
        yield opened


def station(number: int) -> MockAnalyzer:
    return MockAnalyzer(replace(DEFAULT_ZPA_BANK, station=number))


# --- Finding analyzers ---------------------------------------------------------------------


async def test_find_the_bench_analyzer(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = bench()
    async with serial_ports(monkeypatch, {"COM8": [mock]}) as opened:
        results = await find_devices(ports=["COM8"])
    assert opened == ["COM8"]
    assert mock.transactions() == PROBE + IDENTIFY + PROBES
    (result,) = results
    assert result.ok
    assert (result.port, result.address, result.baudrate) == ("COM8", 1, 38_400)
    assert result.protocol is ProtocolKind.MODBUS_RTU
    assert result.model == "ZPA"
    assert result.error is None
    assert result.device_info is not None
    assert result.device_info.serial_number == "N8A0259T"
    assert result.elapsed_s > 0


async def test_without_identifying_one_read_per_station(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = bench()
    async with serial_ports(monkeypatch, {"COM8": [mock]}):
        (result,) = await find_devices(ports=["COM8"], identify=False)
    assert mock.transactions() == PROBE
    assert result.ok
    assert result.model == "ZPA"
    assert result.device_info is None


async def test_a_sweep_of_stations(monkeypatch: pytest.MonkeyPatch) -> None:
    async with serial_ports(monkeypatch, {"COM8": [station(1), station(3)]}):
        results = await find_devices(ports=["COM8"], addresses=(3, 1, 1, 2), **QUICK)
    assert [(r.address, r.ok) for r in results] == [(3, True), (1, True), (2, False)]
    silent = results[2]
    assert isinstance(silent.error, FujiModbusTimeoutError)
    assert (silent.protocol, silent.model, silent.device_info) == (None, None, None)
    assert silent.baudrate == 38_400


async def test_a_station_that_names_another_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = bench()
    mock.set_register("identity.type_code", encode_chars("ABC", 26))
    async with serial_ports(monkeypatch, {"COM8": [mock]}):
        (result,) = await find_devices(ports=["COM8"])
    assert not result.ok
    assert isinstance(result.error, FujiProtocolUnsupportedError)
    assert "answered 'ABC'; it is not a zp analyzer" in str(result.error)
    assert result.error.context.address == 1


async def test_a_station_that_answers_something_else(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = bench()
    mock.set_words(ZP_PROFILE.registry.resolve("identity.type_code").table, 0x0448, (0x1234,) * 3)
    async with serial_ports(monkeypatch, {"COM8": [mock]}):
        (result,) = await find_devices(ports=["COM8"])
    assert not result.ok
    assert "answered 1234h 1234h 1234h" in str(result.error)


async def test_a_modbus_device_that_is_not_an_analyzer(monkeypatch: pytest.MonkeyPatch) -> None:
    other = MockAnalyzer(replace(DEFAULT_ZPA_BANK, regions=(MockRegion(FC04, 0x0000, 0x00C1),)))
    async with serial_ports(monkeypatch, {"COM8": [other]}):
        (result,) = await find_devices(ports=["COM8"])
    assert not result.ok
    assert isinstance(result.error, FujiProtocolUnsupportedError)
    assert isinstance(result.error.__cause__, FujiModbusIllegalDataAddressError)
    assert "with a Modbus exception" in str(result.error)


async def test_a_station_whose_replies_arrive_damaged(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = bench()
    mock.inject(FaultKind.CORRUPT_CRC, times=None)
    async with serial_ports(monkeypatch, {"COM8": [mock]}):
        (result,) = await find_devices(ports=["COM8"], **QUICK)
    assert not result.ok
    assert isinstance(result.error, FujiFrameError)


async def test_an_analyzer_that_fails_identification(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = bench()
    mock.inject(FaultKind.DROP, times=None, when=when(FC04, 0x0425))
    async with serial_ports(monkeypatch, {"COM8": [mock]}):
        (result,) = await find_devices(ports=["COM8"], **QUICK)
    assert result.ok
    assert result.model == "ZPA"
    assert result.device_info is None
    assert isinstance(result.error, FujiModbusTimeoutError)


async def test_a_port_that_will_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async with serial_ports(monkeypatch, {}):
        results = await find_devices(ports=["COM9"], addresses=(1, 2))
    assert [r.address for r in results] == [1, 2]
    for result in results:
        assert not result.ok
        assert result.port == "COM9"
        assert result.baudrate is None
        assert isinstance(result.error, FujiConnectionError)


async def test_ports_are_scanned_in_the_order_given(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = {"COM8": [bench()], "COM10": [station(2)]}
    async with serial_ports(monkeypatch, lines):
        results = await find_devices(
            ports=["COM10", "COM8"], addresses=(1, 2), max_concurrency=1, **QUICK
        )
    assert [(r.port, r.address, r.ok) for r in results] == [
        ("COM10", 1, False),
        ("COM10", 2, True),
        ("COM8", 1, True),
        ("COM8", 2, False),
    ]


async def test_each_port_is_scanned_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async with serial_ports(monkeypatch, {"COM8": [bench()]}) as opened:
        results = await find_devices(ports=["COM8", " COM8 ", "COM8"], identify=False)
    assert opened == ["COM8"]
    assert [(r.port, r.ok) for r in results] == [("COM8", True)]


async def test_every_host_port_when_none_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_ports() -> list[SimpleNamespace]:
        return [SimpleNamespace(device="COM8")]

    monkeypatch.setattr(discovery, "list_serial_ports", list_ports)
    async with serial_ports(monkeypatch, {"COM8": [bench()]}) as opened:
        results = await find_devices(identify=False)
    assert opened == ["COM8"]
    assert [r.ok for r in results] == [True]


async def test_a_host_whose_ports_cannot_be_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_ports() -> list[SimpleNamespace]:
        raise OSError("no access")

    monkeypatch.setattr(discovery, "list_serial_ports", list_ports)
    with pytest.raises(FujiConnectionError, match="cannot list"):
        await find_devices()


async def test_a_second_profile_probes_only_what_the_first_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with serial_ports(monkeypatch, {"COM8": [mock]}, opens=2) as opened:
        results = await find_devices(
            ports=["COM8"],
            addresses=(1, 2),
            profiles=(ZP_PROFILE, ZP_PROFILE),
            identify=False,
            **QUICK,
        )
    assert opened == ["COM8", "COM8"]
    assert mock.transactions() == PROBE
    assert [r.ok for r in results] == [True, False]


async def test_a_second_profile_is_not_tried_when_all_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serial_ports(monkeypatch, {"COM8": [bench()]}, opens=2) as opened:
        await find_devices(ports=["COM8"], profiles=(ZP_PROFILE, ZP_PROFILE), identify=False)
    assert opened == ["COM8"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"addresses": ()}, "non-empty sequence"),
        ({"addresses": "1"}, "non-empty sequence"),
        ({"addresses": (0,)}, "station numbers are 1-31"),
        ({"addresses": (32,)}, "station numbers are 1-31"),
        ({"addresses": (True,)}, "station numbers are 1-31"),
        ({"profiles": ()}, "at least one"),
        ({"per_probe_timeout_s": 0}, "per_probe_timeout_s"),
        ({"per_probe_timeout_s": float("inf")}, "per_probe_timeout_s"),
        ({"per_probe_timeout_s": 61}, "per_probe_timeout_s"),
        ({"max_concurrency": 0}, "max_concurrency"),
        ({"max_concurrency": True}, "max_concurrency"),
        ({"ports": "COM8"}, "not one string"),
        ({"ports": [""]}, "non-empty strings"),
        ({"ports": [8]}, "non-empty strings"),
    ],
)
async def test_invalid_arguments(kwargs: dict[str, Any], match: str) -> None:
    arguments: dict[str, Any] = {"ports": ["COM8"], **kwargs}
    with pytest.raises(FujiValidationError, match=match):
        await find_devices(**arguments)


# --- Results and summaries -----------------------------------------------------------------


def row(port: str, address: int, *, ok: bool, error: Exception | None = None) -> DiscoveryResult:
    return DiscoveryResult(
        ok=ok,
        port=port,
        address=address,
        baudrate=38_400,
        protocol=ProtocolKind.MODBUS_RTU if ok else None,
        device_info=None,
        error=error,  # type: ignore[arg-type]
        elapsed_s=0.5,
    )


def test_the_unified_result_fields() -> None:
    assert [f.name for f in dataclasses.fields(DiscoveryResult)][:8] == [
        "ok",
        "port",
        "address",
        "baudrate",
        "protocol",
        "device_info",
        "error",
        "elapsed_s",
    ]


def test_summarize_discovery() -> None:
    timeout = FujiModbusTimeoutError("no reply")
    closed = FujiConnectionError("cannot open")
    summaries = summarize_discovery(
        [
            row("COM8", 1, ok=True),
            row("COM8", 2, ok=False, error=timeout),
            row("COM8", 3, ok=True),
            row("COM9", 1, ok=False, error=closed),
            row("COM9", 2, ok=False, error=timeout),
        ]
    )
    assert summaries == [
        DiscoverySummary("COM8", ok=True, addresses=(1, 3), probed=3, error=None, elapsed_s=1.5),
        DiscoverySummary("COM9", ok=False, addresses=(), probed=2, error=closed, elapsed_s=1.0),
    ]


@pytest.mark.parametrize(
    ("text", "model"),
    [("ZPA", "ZPA"), ("ZPG", "ZPG"), ("ZP1", None), ("XYZ", None), ("ZP", None)],
)
def test_recognize_zp(text: str, model: str | None) -> None:
    assert recognize_zp(encode_chars(text, len(text))) == model


def test_recognize_zp_needs_characters() -> None:
    assert recognize_zp((0x1234, 0x0050, 0x0041)) is None
