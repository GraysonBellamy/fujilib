"""Wiring a client to the simulator, and loading register banks (design §10).

- :func:`mock_transport` puts :class:`~fujilib.testing.mock.MockAnalyzer`
  stations on one end of ``anyserial.testing.serial_port_pair()`` and returns
  a :class:`~fujilib.transport.serial.SerialTransport` over the other end.
  Both ends are real ``SerialPort`` objects, so ``anymodbus`` drains and
  clears input exactly as it does on hardware.
- :func:`mock_port` adds the :class:`~fujilib.protocol.modbus.port.ModbusPort`;
  :func:`mock_analyzer_pair` goes one step further, to one station's client.
- :func:`fake_transport` replays an arrow fixture byte for byte.
- :data:`DEFAULT_ZPA_BANK` is the sanitized bench bank: the documented
  register blocks of the bench ZPA plus its observed clock and A/D block, with
  the factory calibration blocks left out (design §13.1 #11).

The pairs use the library's timing defaults, except that the one-shot startup
settle is 0: it exists for RS-485 adapters, and there is none here.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from fujilib.config import DEFAULTS
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.testing.arrow import parse_arrow_fixture, replay_script
from fujilib.testing.mock import (
    ExceptionProfile,
    MockAnalyzer,
    MockAnalyzerConfig,
    MockLine,
    MockRegion,
    zp_readable_regions,
)
from fujilib.transport.base import FUJI_BAUDRATE, SerialSettings
from fujilib.transport.fake import FakeTransport
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from fujilib.protocol.modbus.client import ModbusClient

__all__ = [
    "BENCH_BANK_PATH",
    "DEFAULT_ZPA_BANK",
    "fake_transport",
    "load_bank",
    "mock_analyzer_pair",
    "mock_port",
    "mock_transport",
]

#: The sanitized bench bank, a register dump ``fuji-decode --dump`` reads.
BENCH_BANK_PATH: Final = Path(__file__).with_name("zpa_bench_documented.json")

_MOCK_LABEL: Final = "mock://zp"


def _words(data: Mapping[str, object], table: str) -> Mapping[int, int]:
    raw = data.get(table, {})
    if not isinstance(raw, dict):
        msg = f"the bank's {table!r} entry is not an address -> word mapping"
        raise TypeError(msg)
    return MappingProxyType({int(str(a), 16): int(w) for a, w in raw.items()})  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def load_bank(
    source: Path | Mapping[str, object] = BENCH_BANK_PATH,
    *,
    station: int | None = None,
    profile: ExceptionProfile = ExceptionProfile.BENCH_1_02,
    regions: tuple[MockRegion, ...] | None = None,
) -> MockAnalyzerConfig:
    """A simulator configuration from a register dump.

    Args:
        source: A dump file, or its parsed JSON: ``input`` and ``holding``
            tables of hex addresses to words, as ``probe_map.py`` writes them.
        station: The station number; defaults to the dump's ``station``, else 1.
        profile: The exception replies to give.
        regions: The readable map; defaults to :func:`zp_readable_regions`.

    Raises:
        TypeError: a table is not a mapping.
    """
    data: Mapping[str, object] = (
        json.loads(source.read_text(encoding="utf-8")) if isinstance(source, Path) else source
    )
    dumped = data.get("station", 1)
    return MockAnalyzerConfig(
        station=station if station is not None else int(str(dumped)),
        profile=profile,
        input=_words(data, "input"),
        holding=_words(data, "holding"),
        regions=regions if regions is not None else zp_readable_regions(),
        description=str(data.get("description", "")),
    )


#: The bench ZPA (CO2 / CO / O2) at station 1, answering as firmware 1.02 did.
DEFAULT_ZPA_BANK: Final = load_bank()


def fake_transport(source: str | Path, *, label: str = "fake://fixture") -> FakeTransport:
    """A :class:`FakeTransport` that replays the arrow fixture ``source`` (a path or its text).

    Raises:
        FujiValidationError: the fixture is malformed, or gives one request two replies.
    """
    return FakeTransport(replay_script(parse_arrow_fixture(source)), label=label)


@asynccontextmanager
async def mock_transport(
    *analyzers: MockAnalyzer, label: str = _MOCK_LABEL
) -> AsyncGenerator[tuple[SerialTransport, MockLine]]:
    """A transport whose line carries ``analyzers``, answered by a :class:`MockLine`.

    Both ends are closed on exit. A :class:`~fujilib.testing.mock.MockWriteViolation`
    raised while serving cancels the ``async with`` block and is raised from it.

    The line is served by a task group, which would wrap any failure in an
    exception group. A single failure, the block's own or the line's, is raised
    as itself, so ``pytest.raises`` works around the whole block.
    """
    config = SerialConfig(baudrate=FUJI_BAUDRATE)
    host, device = serial_port_pair(
        config_a=config, config_b=config, path_a=label, path_b=f"{label}/analyzer"
    )
    transport = SerialTransport(host, SerialSettings(port=label))
    line = MockLine(*analyzers)
    try:
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(line.serve, device)
            try:
                yield transport, line
            finally:
                tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await transport.aclose()
            await device.aclose()


@asynccontextmanager
async def mock_port(
    *analyzers: MockAnalyzer,
    request_timeout: float = DEFAULTS.request_timeout_s,
    inter_frame_idle: float = DEFAULTS.inter_frame_idle_s,
    startup_settle: float = 0.0,
    read_retries: int = DEFAULTS.read_retries,
    resync_window: float = DEFAULTS.resync_window_s,
) -> AsyncGenerator[tuple[ModbusPort, MockLine]]:
    """A :class:`ModbusPort` on a line carrying ``analyzers``."""
    async with (
        mock_transport(*analyzers) as (transport, line),
        ModbusPort(
            transport,
            request_timeout=request_timeout,
            inter_frame_idle=inter_frame_idle,
            startup_settle=startup_settle,
            read_retries=read_retries,
            resync_window=resync_window,
        ) as port,
    ):
        yield port, line


@asynccontextmanager
async def mock_analyzer_pair(
    config: MockAnalyzerConfig | None = None,
    *,
    request_timeout: float = DEFAULTS.request_timeout_s,
    inter_frame_idle: float = DEFAULTS.inter_frame_idle_s,
    startup_settle: float = 0.0,
    read_retries: int = DEFAULTS.read_retries,
    resync_window: float = DEFAULTS.resync_window_s,
) -> AsyncGenerator[tuple[ModbusClient, MockAnalyzer]]:
    """A client talking to one simulated analyzer, :data:`DEFAULT_ZPA_BANK` by default."""
    analyzer = MockAnalyzer(config if config is not None else DEFAULT_ZPA_BANK)
    async with mock_port(
        analyzer,
        request_timeout=request_timeout,
        inter_frame_idle=inter_frame_idle,
        startup_settle=startup_settle,
        read_retries=read_retries,
        resync_window=resync_window,
    ) as (port, _line):
        yield port.client(analyzer.station), analyzer
