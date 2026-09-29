# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `Sample` carries `channels`, the channels its row has columns for, and
  `sample_to_row(sample)` uses them by default. The recorder sets them on every
  sample from the channels established when the recording starts, so a failed
  poll's row has the same keys as any other.
- `PollSourceAdapter` also describes its analyzer (`layout()`: station, protocol,
  established channels, whether it can be reopened) and can reopen it
  (`reconnect()`).
- `read_metadata()` reads the current ranges itself (one more transaction), so the
  settings snapshot no longer depends on an earlier poll.
- Require `anyserial>=0.2.0`, which gives every spelling of a port one canonical name,
  opens `\\?\` paths unchanged, and reads a real Windows COM port under trio.
- Require `anymodbus>=0.3,<0.4` and `anyio>=4.14`. `anymodbus` 0.3 measures the
  inter-frame gap from the end of every attempt, checks each reply against its
  request, discards late replies during a quiet window, and reports every attempt to
  an observer (design §4.2-§4.7).
- `fujilib.testing` is a package, and the sanitized bench bank is its package data
  (`fujilib/testing/zpa_bench_documented.json`), so `DEFAULT_ZPA_BANK` works from an
  installed wheel.

### Added

- The recorder: `record()` polls one or more analyzers at a fixed rate into a
  bounded stream of per-tick batches, yielding a `Recording` with a live
  `AcquisitionSummary`. Ticks follow an absolute schedule; slots a slow poll
  overran are skipped and counted late, never caught up in a burst. A failed poll
  is an error sample, so gaps are recorded. A full buffer waits (`BLOCK`) or drops
  whole batches (`DROP_NEWEST`, `DROP_OLDEST`). A connection failure ends the
  recording after its batch is delivered, and leaving the block raises it; with a
  `ReconnectPolicy` the recording rides it out, reopening the analyzer on a
  back-off schedule. The summary is finished however the recording stops.
- `Analyzer.reopen()`: opens a port that `open_device` opened by name again after
  a connection failure, identifies the analyzer, and requires it to be the same
  one (serial number and type code). What the session learned is kept, and its
  traffic counters and recovered-error count run on across it. Reopens are taken
  one at a time, `close()` waits for one in progress, and a closed analyzer
  cannot be reopened.
- Sinks: `InMemorySink`, `CsvSink` and `ParquetSink` (the new `parquet` extra),
  on a common `BaseSink`, and `pipe()`. Columns are fixed before the first row
  from `row_columns()` (`SchemaLock`), never inferred from values, and a sample
  with a channel the columns lack is refused. File I/O runs in worker threads. CSV
  quotes text, so an empty field is `None` and `""` is empty text. Parquet gathers
  rows into row groups of 1,000, so a long recording keeps its memory flat. `pipe()` writes
  in groups, flushes on a timer while the stream is idle, and writes what it holds
  when stopped or cancelled.
- Blocking recording: `fujilib.sync.record()`, `pipe()`, `PollSourceAdapter`,
  `SyncRecording`, `SyncAnalyzer.reopen()` and the `SyncInMemorySink`,
  `SyncCsvSink` and `SyncParquetSink` sinks.
- `fuji-stream` prints each poll as text, CSV or JSON lines; `fuji-capture`
  records to CSV or Parquet with a `fujilib-capture/1` metadata document beside
  the data (identity, metadata, arguments, versions, and how the recording ended
  with its counters); `fuji-diag timing` measures the gap the analyzer needs
  between requests. Ctrl-C stops a recording command cleanly, with exit code 0.
- Guides for recording, the commands and measurement quality (including the
  statement that Modbus O2 is not validated for oxygen-consumption calorimetry),
  API pages for `fujilib.streaming` and `fujilib.sinks`, and
  `examples/record_to_parquet.py`.
- `scripts/soak_monitor.py` and `scripts/check_soak.py` for long recordings on
  the bench.
- `open_device()`: opens a serial port (or takes an open transport), attaches to a
  station and identifies the analyzer; everything it opened is closed again if that
  fails or is cancelled, and a caller's transport is never closed by it.
- The `Analyzer` facade: `poll()`, `read_channel()`, `status()`, `channel_status()`,
  `identify()`, `snapshot()`, `read_ranges()`, `read_metadata()`, `read_clock()`,
  `read_adc()`, `reprobe()`, `read_error_log()`, `read_calibration_log()`,
  `read_parameter()`, `read_parameters()` and `read_settings()`. Every method takes a
  `timeout=` that bounds the whole operation, including the wait for the port.
  Arguments are checked before anything is sent.
- `Session`, the one path to the wire: it refuses calls on a closed or broken session
  and, before any I/O, reads of a capability a probe found absent; holds the port's
  lock for a whole operation; keeps the last error and the recovered-error count;
  and keeps what it has learned current. A channel that reads non-zero joins the
  established channels in the poll that shows it, a change of current range makes the
  range tables stale, and a capability's availability follows every read of it.
- Discovery: `find_devices()` probes stations for ZP analyzers, read-only, one read
  each, ports in parallel; `DiscoveryResult`, `DiscoverySummary` and
  `summarize_discovery()`. A probe that fails, or a port that will not open, is a row,
  not an exception.
- `DeviceProfile` and `ZP_PROFILE`; `DeviceResult` and `PollSourceAdapter`.
- `fujilib.sync`: `Fuji.open()`, `SyncAnalyzer`, a blocking `find_devices()` and
  `SyncPortal`, held to the async API by a parity test.
- `fuji-read`, `fuji-discover` and `fuji-configure dump`, read-only. `fuji-read` and
  `fuji-configure` take `--fixture` with a register bank, or `bench`, to run against the
  simulated analyzer.
- Read-only hardware tests of the facade, discovery, the blocking facade and the
  commands (`tests/hardware/`), and `docs/hardware-test-day.md`. On the bench unit all
  45 pass under asyncio and trio (`docs/protocol-findings.md` §11).
- Quickstarts for the async and the blocking API.
- `fujilib.transport`: the `Transport` contract; `SerialTransport`, which exposes the
  real `anyserial.SerialPort` so `anymodbus` keeps drain-after-send and input reset;
  `FakeTransport` for byte-exact fixture replay. A port opens under `anyserial`'s
  canonical name, so `COM8`, `com8` and `\\.\COM8` are one port.
- `fujilib.protocol.modbus.port.ModbusPort`: one `anymodbus` bus per serial port, an
  operation lock, one client per station (1-31), and refusal of a second port on one
  transport. The bus waits out the startup settle and the inter-frame gap, retries
  reads (never writes) lost or damaged in transit, and after a cancelled, timed-out,
  damaged or mismatched attempt keeps a 0.1 s quiet window in which late bytes are
  discarded, so a late reply cannot answer the next read. A request whose deadline
  ends inside that window is refused before any I/O.
- `fujilib.protocol.modbus.client.ModbusClient`: block reads and read plans with
  argument checks before any I/O. Each block's `TransferTiming` runs from when its
  request had been sent (after the gap) to the reply, and the traffic counters come from
  `anymodbus`'s report of every attempt: failures by kind, retries and
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
- Read-only hardware tests of the client and read procedures
  (`tests/hardware/test_hardware_client.py`, gated by `FUJILIB_ENABLE_HARDWARE_TESTS=1`
  and `FUJILIB_HARDWARE_PORT`), and `scripts/probe_client.py`, which measures the client
  on a real analyzer. On the bench unit every read procedure works, 300 polls ran at
  7.78 Hz with no failure, and the quiet window turns a lost request into a clean read
  (`docs/protocol-findings.md` §10). The hardware tests pass under asyncio and trio.
- `fujilib.testing`: `MockAnalyzer` stations on a `MockLine` (`anymodbus`'s
  `MockServer`) over a real serial port pair, with the manual's and the bench unit's
  exception replies, per-request reply
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
