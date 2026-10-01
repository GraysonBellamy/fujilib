"""Test helpers: fixtures, model builders, the simulated analyzer, the bench bank (design §10).

- :mod:`~fujilib.testing.arrow` — the family's arrow-format frame fixtures.
- :mod:`~fujilib.testing.frames` — builders for synthetic readings, statuses
  and frames, for code that consumes the models without a line.
- :mod:`~fujilib.testing.mock` — :class:`MockAnalyzer` stations on a
  :class:`MockLine`, with exception profiles and scripted faults.
- :mod:`~fujilib.testing.pair` — a client wired to the simulator over a real
  serial port pair, fixture replay, and :data:`DEFAULT_ZPA_BANK`.
"""

from __future__ import annotations

from fujilib.testing.arrow import Exchange, hex_to_bytes, parse_arrow_fixture, replay_script
from fujilib.testing.frames import analyzer, bench_readings, frame, reading, status, timing
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
    "analyzer",
    "bench_readings",
    "fake_transport",
    "frame",
    "hex_to_bytes",
    "load_bank",
    "mock_analyzer_pair",
    "mock_port",
    "mock_transport",
    "parse_arrow_fixture",
    "reading",
    "replay_script",
    "status",
    "timing",
    "zp_readable_regions",
]
