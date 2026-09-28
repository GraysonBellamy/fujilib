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

- Project scaffolding from the `*lib` family skeleton: packaging (hatchling +
  hatch-vcs), CI / docs / release workflows, ruff / mypy / pyright / pytest
  configuration, pre-commit hooks and the documentation site.
- `fujilib.errors`: the `FujiError` hierarchy and the frozen `ErrorContext`
  (design §9).
- The architecture and phased plan (`docs/design.md`) and the read-only bench
  findings (`docs/protocol-findings.md`).
- Read-only bench probe scripts (`scripts/probe_*.py`) and
  `scripts/extract_manuals.py`, which extracts searchable text from the vendor
  manuals.
