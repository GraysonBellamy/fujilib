---
description: API reference for fujilib, auto-generated from source docstrings via mkdocstrings-python.
---

# API reference

Auto-generated from source docstrings via
[mkdocstrings-python](https://mkdocstrings.github.io/python/).

- [`fujilib.registry`](registry.md) — the register map, regions, the write envelope,
  type codes, channels, units and enums.
- [`fujilib.devices`](devices.md) — data models, pure decoders, read procedures,
  capability flags and snapshots.
- [`fujilib.protocol`](protocol.md) — the register-word codec, the read planner, the
  Modbus port and client, and the error map.
- [`fujilib.transport`](transport.md) — the transport contract, `SerialSettings`, the
  serial transport, the scripted fake and canonical port names.
- [Samples and rows](samples.md) — `Sample`, `sample_to_row()` and `to_pint()`.
- [`fujilib.testing`](testing.md) — arrow-format frame fixtures, the simulated analyzer
  and the bench bank.
- [`fujilib.errors`](errors.md) — typed exception hierarchy and `ErrorContext`.
