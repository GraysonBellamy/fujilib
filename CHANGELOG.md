# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Require `anymodbus>=0.2.1`, which measures the inter-frame idle gap from the
  end of every transaction, including exception replies, checksum errors,
  timeouts and cancellations (design §4.7).
- `fujilib.testing` is a package, and the sanitized bench bank is its package data
  (`fujilib/testing/zpa_bench_documented.json`), so `DEFAULT_ZPA_BANK` works from an
  installed wheel.

### Added

- `fujilib.transport`: the `Transport` contract; `SerialTransport`, which exposes the
  real `anyserial.SerialPort` so `anymodbus` keeps drain-after-send and input reset;
  `FakeTransport` for byte-exact fixture replay; `canonical_port()`, under which
  `COM8`, `com8` and `\\.\COM8` are one port.
- `fujilib.protocol.modbus.port.ModbusPort`: one `anymodbus` bus per serial port, an
  operation lock, one client per station (1-31), and refusal of a second port on one
  transport. Clients wait out the inter-frame gap and the one-shot startup settle
  themselves, so a transaction's timing starts when its request goes out. After a
  cancelled, timed-out, garbled or mismatched transaction, no request goes out until
  a quiet window has passed, so a late reply cannot answer the next read.
- `fujilib.protocol.modbus.client.ModbusClient`: block reads and read plans with
  argument checks before any I/O, a word-count check `anymodbus` does not make,
  per-block `TransferTiming`, and retries of lost, garbled, mismatched or short
  replies (never of exception replies), counted by kind with
  `recoverable_error_count`. FC06 and FC10 write primitives check the frozen write
  envelope last and are never retried. Once a request may have gone out, any failure
  other than an exception reply is reported as an unknown outcome, including a deadline
  that a caller enforces around the call. A deadline that expires mid-plan keeps the
  blocks already read. Failures are translated to the `FujiError` hierarchy at one
  boundary.
- `fujilib.devices.reads`: poll, status, ranges, identity with the capability
  probes, metadata, settings, named registers, both logs (read again once if an
  entry arrives mid-read), clock and A/D values, each as an exact list of
  transactions.
- `fujilib.config.DEFAULTS`: the timing and retry defaults, each with the
  measurement or manual statement it rests on; operation deadlines that cover queue
  time and retries.
- `fujilib.testing`: `MockAnalyzer` stations on a `MockLine` over a real serial port
  pair, with the manual's and the bench unit's exception replies, per-request reply
  faults (drop, delay, bad CRC, wrong count, wrong function code, garbage, exception),
  a request log with arrival times, and a hard failure on any write fujilib must
  never send; `mock_transport()`, `mock_port()`, `mock_analyzer_pair()`,
  `fake_transport()` and `load_bank()`.

- `fujilib.registry`: the ZP-series register map (343 registers and the error and
  calibration logs), declared from the manual's stride patterns and validated at import;
  the documented and observed read regions; the frozen write envelope and the four
  operation commands; the ZPA type-code decoder and channel-layout rule; channels,
  gases, label sources, units and the manual's enumerated values.
- `docs/registers.md`, generated from the registry by `scripts/gen_register_docs.py` and
  checked in CI.
- The register-word codec (signed words, low-word-first long words, BCD, one character
  per register, decimal-point scaling) and the region-aware read planner, whose hot paths
  are planned at import.
- The frozen data models, including `Reading` with a closed validity vocabulary
  (`ReadingState`), `Frame`, the settings snapshot, logs with partial timestamps and the
  unified-API snapshots; pure decoders from register banks to these models.
- `Sample` (one per poll, carrying a whole `Frame`) and `sample_to_row()`, which
  flattens it into one wide row of scalar columns with the same keys on error rows;
  `row_columns()` gives each column's type. `to_pint()` returns unit strings for pint.
- `fuji-decode`, which decodes MODBUS frames, arrow fixtures and register dumps offline.
- `fujilib.testing`: the arrow-format fixture parser. The MODBUS manual's worked frames
  and a sanitized bench register bank are test fixtures.
- Project scaffolding from the `*lib` family skeleton: packaging (hatchling +
  hatch-vcs), CI / docs / release workflows, ruff / mypy / pyright / pytest
  configuration, pre-commit hooks and the documentation site.
- `fujilib.errors`: the `FujiError` hierarchy and the frozen `ErrorContext`
  (design §9).
- The architecture and phased plan (`docs/design.md`) and the read-only bench
  findings (`docs/protocol-findings.md`).
- `scripts/probe_link.py --mode pairs`: randomized link-timing trials across all
  four normal/exception reply pairings, with each gap measured from the previous
  reply and every trial kept. Its results are in `docs/protocol-findings.md` §6.3.
- Read-only bench probe scripts (`scripts/probe_*.py`) and
  `scripts/extract_manuals.py`, which extracts searchable text from the vendor
  manuals.
