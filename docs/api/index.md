---
description: API reference for fujilib, auto-generated from source docstrings via mkdocstrings-python.
---

# API reference

Auto-generated from source docstrings via
[mkdocstrings-python](https://mkdocstrings.github.io/python/).

- [`fujilib.registry`](registry.md) — the register map, regions, the write envelope,
  type codes, channels, units and enums.
- [`fujilib.devices`](devices.md) — `open_device`, the `Analyzer` facade, its session,
  discovery and profiles; data models, pure decoders, read procedures, capability
  flags and snapshots.
- [`fujilib.sync`](sync.md) — the blocking facade: `Fuji.open`, `SyncAnalyzer`,
  discovery, recording and sinks, and `SyncPortal`.
- [`fujilib.protocol`](protocol.md) — the register-word codec, the read planner, the
  Modbus port and client, and the error map.
- [`fujilib.transport`](transport.md) — the transport contract, `SerialSettings`, the
  serial transport and the scripted fake.
- [`fujilib.streaming`](streaming.md) — `Sample`, `DeviceResult`, poll sources,
  the recorder (`record()`, `Recording`, `AcquisitionSummary`) and `to_pint()`.
- [`fujilib.sinks`](sinks.md) — `sample_to_row()`, `row_columns()`, the memory,
  CSV and Parquet sinks, and `pipe()`.
- [`fujilib.testing`](testing.md) — arrow-format frame fixtures, the simulated analyzer
  and the bench bank.
- [`fujilib.errors`](errors.md) — typed exception hierarchy and `ErrorContext`.
