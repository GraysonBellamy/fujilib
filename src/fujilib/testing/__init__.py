"""Test helpers: arrow fixtures, the simulated analyzer and the bench bank (design §10).

- :mod:`~fujilib.testing.arrow` — the family's arrow-format frame fixtures.
- :mod:`~fujilib.testing.mock` — :class:`MockAnalyzer` stations on a
  :class:`MockLine`, with exception profiles and scripted faults.
- :mod:`~fujilib.testing.pair` — a client wired to the simulator over a real
  serial port pair, fixture replay, and :data:`DEFAULT_ZPA_BANK`.
"""

from __future__ import annotations

from fujilib.testing.arrow import Exchange, hex_to_bytes, parse_arrow_fixture, replay_script
from fujilib.testing.mock import (
    ExceptionProfile,
    Fault,
    FaultKind,
    MockAnalyzer,
    MockAnalyzerConfig,
    MockCalibration,
    MockExchange,
    MockLine,
    MockRegion,
    MockRequest,
    MockWriteViolation,
    zp_readable_regions,
)
from fujilib.testing.pair import (
    BENCH_BANK_PATH,
    DEFAULT_ZPA_BANK,
    fake_transport,
    load_bank,
    mock_analyzer_pair,
    mock_port,
    mock_transport,
)

__all__ = [
    "BENCH_BANK_PATH",
    "DEFAULT_ZPA_BANK",
    "ExceptionProfile",
    "Exchange",
    "Fault",
    "FaultKind",
    "MockAnalyzer",
    "MockAnalyzerConfig",
    "MockCalibration",
    "MockExchange",
    "MockLine",
    "MockRegion",
    "MockRequest",
    "MockWriteViolation",
    "fake_transport",
    "hex_to_bytes",
    "load_bank",
    "mock_analyzer_pair",
    "mock_port",
    "mock_transport",
    "parse_arrow_fixture",
    "replay_script",
    "zp_readable_regions",
]
