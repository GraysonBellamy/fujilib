# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Require `anymodbus>=0.2.1`, which measures the inter-frame idle gap from the
  end of every transaction, including exception replies, checksum errors,
  timeouts and cancellations (design §4.7).

### Added

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
