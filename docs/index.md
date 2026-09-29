---
description: Async Python driver for Fuji Electric ZP-series NDIR gas analyzers over MODBUS RTU.
---

# fujilib

Async-first Python driver for [Fuji Electric](https://www.fujielectric.com/)
**ZP-series NDIR gas analyzers**: the ZPA, and the ZPB / ZPG / ZPAJ / ZPG3E
models that share its MODBUS map. It is built on
[`anyserial`](https://pypi.org/project/anyserial/) and
[`anymodbus`](https://pypi.org/project/anymodbus/).

!!! warning "Pre-alpha"
    Nothing has been released yet. The read-only API works: `open_device()`,
    identification, polls with validity, metadata, settings, logs, discovery,
    [recording](recording.md) to memory, CSV or Parquet, a blocking facade and
    the [`fuji-*` commands](cli.md). Writes are still to come; the
    [design](design.md) lays out the plan. Start with the
    [async quickstart](quickstart-async.md).

The authoritative architectural document is the [Design](design.md). What the
development analyzer actually does on the wire is recorded in
[Protocol findings](protocol-findings.md).

- **Channel-oriented reads.** A full poll of every channel and the analyzer's
  status takes two Modbus transactions.
- **Validity and provenance on every value.** Hold, calibration and error state,
  and where each gas label came from, travel with each reading.
- **Writes restricted to a frozen envelope** of documented user settings and
  operation commands, behind `confirm=True` and read-back verification.
- **No hardware needed** to develop or test: a simulated analyzer drives the
  full stack in CI.

!!! note "Oxygen"
    Over Modbus, O2 arrives with the display's resolution: 0.01 vol% per step on
    the development unit. Modbus O2 is **not validated for oxygen-consumption
    calorimetry**; see [Measurement quality](measurement-quality.md).
