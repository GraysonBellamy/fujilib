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

from fujilib.devices.analyzer import Analyzer
from fujilib.devices.profile import ZP_PROFILE
from fujilib.devices.session import Session
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

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

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
    **timing: Any,
) -> AsyncGenerator[tuple[Analyzer, MockLine]]:
    """An :class:`Analyzer` on a line carrying ``analyzers``; identified unless asked not to.

    The analyzers' request logs are cleared after identification, so a test
    sees only its own transactions.
    """
    async with mock_transport(*analyzers) as (transport, line):
        port = ModbusPort(transport, **{**FAST, **timing})
        analyzer = Analyzer(
            Session(port, address=address, profile=ZP_PROFILE, channel_map=channel_map)
        )
        try:
            if identify:
                await analyzer.identify()
            for station in analyzers:
                station.clear()
            yield analyzer, line
        finally:
            await analyzer.close()
