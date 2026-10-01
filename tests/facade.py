"""The analyzer facade wired to the simulator, for tests.

``open_device`` uses the library's timing defaults, which suit hardware but
make a simulated test slow. :func:`analyzer_on` builds the same
:class:`Analyzer` over a :class:`ModbusPort` with no inter-frame idle and a
short timeout, so tests can count transactions quickly and exactly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

import anyio
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from fujilib.config import DEFAULTS
from fujilib.devices.analyzer import Analyzer
from fujilib.devices.capability import Capability
from fujilib.devices.profile import ZP_PROFILE
from fujilib.devices.session import Session
from fujilib.errors import FujiConnectionError
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.registers import CALIBRATION_LOG
from fujilib.testing import (
    DEFAULT_ZPA_BANK,
    MockAnalyzer,
    MockAnalyzerConfig,
    MockLine,
    MockRequest,
    mock_transport,
    zp_readable_regions,
)
from fujilib.transport.base import FUJI_BAUDRATE, SerialSettings
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

    from anyio.abc import TaskGroup
    from anyserial import SerialPort

FC03, FC04 = 0x03, 0x04
#: The bench rig's asserted channel map (design §13.1 #14).
ASSERTED: Final[Mapping[ChannelId, Gas]] = {
    ChannelId.CH1: Gas.CO2,
    ChannelId.CH2: Gas.CO,
    ChannelId.CH3: Gas.O2,
}
#: Long enough that a stall under load never forces a retry, since tests count transactions.
FAST: Final[Mapping[str, Any]] = {
    "inter_frame_idle": 0.0,
    "startup_settle": 0.0,
    "request_timeout": 0.25,
    "resync_window": 0.01,
}
POLL = [(FC04, 0x0000, 61), (FC04, 0x0083, 60)]
IDENTIFY = [(FC04, 0x0425, 35), (FC04, 0x0448, 34), (FC04, 0x0000, 42)]
PROBES = [(FC04, 0x03E8, 49), (FC04, 0x047A, 3), (FC04, 0x1000, 9)]
RANGES = [(FC04, 0x0425, 35)]
SETTINGS = [(FC03, 0x0000, 64), (FC03, 0x0040, 64), (FC03, 0x0080, 44)]
METADATA = [(FC03, 0x0000, 64), (FC03, 0x0040, 64), (FC03, 0x0080, 36), (FC04, 0x0025, 5)]
CLOCK = [(FC04, 0x03E8, 7)]


def bench(config: MockAnalyzerConfig | None = None) -> MockAnalyzer:
    """The bench ZPA at station 1, or ``config``."""
    return MockAnalyzer(config if config is not None else DEFAULT_ZPA_BANK)


def documented_only() -> MockAnalyzerConfig:
    """The bench bank on the manual's map: no clock and no A/D block."""
    return replace(DEFAULT_ZPA_BANK, regions=zp_readable_regions(observed=False))


def newer_firmware() -> MockAnalyzerConfig:
    """The bench bank on firmware 2.24: digits 27-29 and a calibration log."""
    config = replace(DEFAULT_ZPA_BANK, regions=zp_readable_regions(firmware_2_24=True))
    words = dict(config.input)
    words.update(zip(range(0x047A, 0x047D), encode_chars("ABC", 3), strict=True))
    for channel in range(1, 6):
        for index in range(CALIBRATION_LOG.records):
            words[CALIBRATION_LOG.record_address(channel, index)] = 0xFFFF
    record = (2, 1, 0x86A0, 0x0001, 15, 9, 28, 14, 30)  # CH2 span 1, 100000 counts, 1.5 %FS
    base = CALIBRATION_LOG.record_address(2, 0)
    words.update(zip(range(base, base + 9), record, strict=True))
    return replace(config, input=words)


def when(fc: int, address: int, count: int | None = None) -> Callable[[MockRequest], bool]:
    """Match requests of ``fc`` at ``address`` (of ``count`` words, when given)."""
    return lambda r: r.function == fc and r.address == address and count in {None, r.count}


@asynccontextmanager
async def analyzer_on(
    *analyzers: MockAnalyzer,
    address: int = 1,
    channel_map: Mapping[ChannelId, Gas] | None = ASSERTED,
    identify: bool = True,
    options: Capability = Capability.NONE,
    **timing: Any,
) -> AsyncGenerator[tuple[Analyzer, MockLine]]:
    """An :class:`Analyzer` on a line carrying ``analyzers``; identified unless asked not to.

    ``options`` are asserted as fitted. The analyzers' request logs are cleared
    after identification, so a test sees only its own transactions.
    """
    async with mock_transport(*analyzers) as (transport, line):
        port = ModbusPort(transport, **{**FAST, **timing})
        analyzer = Analyzer(
            Session(
                port,
                address=address,
                profile=ZP_PROFILE,
                channel_map=channel_map,
                options=options,
            )
        )
        try:
            if identify:
                await analyzer.identify()
            for station in analyzers:
                station.clear()
            yield analyzer, line
        finally:
            await analyzer.close()


class Cable:
    """A simulated cable between a port and a line of analyzers, which a test can pull out.

    Each :meth:`plug` is a new serial pair and line, as a port opened again
    after a real adapter was replugged. While unplugged, opening fails as a
    missing port does.
    """

    def __init__(self, tg: TaskGroup, analyzers: tuple[MockAnalyzer, ...], timing: Any) -> None:
        self._tg = tg
        self._analyzers = analyzers
        self._timing = {**FAST, **timing}
        self._device: SerialPort | None = None
        self._scope: anyio.CancelScope | None = None
        self.connected = True
        self.plugs = 0

    async def plug(self) -> ModbusPort:
        """Open the port: a new pair and line (the session's reopener)."""
        return ModbusPort(await self.open_transport(), owns_transport=True, **self._timing)

    async def open_transport(self) -> SerialTransport:
        """A transport on a new pair and line; the previous line stops."""
        if not self.connected:
            msg = "the adapter is unplugged"
            raise FujiConnectionError(msg)
        await self._stop_line()
        self.plugs += 1
        config = SerialConfig(baudrate=FUJI_BAUDRATE)
        host, device = serial_port_pair(
            config_a=config, config_b=config, path_a="mock://cable", path_b="mock://cable/line"
        )
        line = MockLine(*self._analyzers)
        scope = anyio.CancelScope()

        async def serve() -> None:
            with scope:
                await line.serve(device)

        _ = self._tg.start_soon(serve)
        self._device, self._scope = device, scope
        return SerialTransport(host, SerialSettings(port="mock://cable"))

    async def unplug(self) -> None:
        """Pull the cable: the line stops and the port fails on its next request."""
        self.connected = False
        await self._stop_line()

    async def _stop_line(self) -> None:
        if self._scope is not None:
            self._scope.cancel()
        if self._device is not None:
            await self._device.aclose()
        self._scope = self._device = None

    def replug(self) -> None:
        """Put the cable back: the next :meth:`plug` succeeds."""
        self.connected = True


@asynccontextmanager
async def replugging(
    *analyzers: MockAnalyzer,
    channel_map: Mapping[ChannelId, Gas] | None = ASSERTED,
    identify: bool = True,
    settle_after_reopen_s: float = DEFAULTS.settle_after_reopen_s,
    **timing: Any,
) -> AsyncGenerator[tuple[Analyzer, Cable]]:
    """An :class:`Analyzer` whose session can be reopened over a :class:`Cable`.

    Identified unless asked not to; the request logs are cleared afterwards.
    """
    async with anyio.create_task_group() as tg:
        cable = Cable(tg, analyzers, timing)
        port = await cable.plug()
        session = Session(
            port,
            address=1,
            profile=ZP_PROFILE,
            channel_map=channel_map,
            reopener=cable.plug,
            settle_after_reopen_s=settle_after_reopen_s,
        )
        analyzer = Analyzer(session)
        try:
            if identify:
                _ = await analyzer.identify()
            for station in analyzers:
                station.clear()
            yield analyzer, cable
        finally:
            await analyzer.close()
            await cable.unplug()
            tg.cancel_scope.cancel()
