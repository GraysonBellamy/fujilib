---
description: Async Python driver for Fuji Electric ZP-series NDIR gas analyzers (ZPA, also sold as the CAI / ENVEA ZPA) over MODBUS RTU.
---

# fujilib

Async-first Python driver for [Fuji Electric](https://www.fujielectric.com/)
**ZP-series NDIR gas analyzers**: the ZPA, and the ZPB / ZPG / ZPAJ / ZPG3E
models that share its MODBUS map. It is built on
[`anyserial`](https://pypi.org/project/anyserial/) and
[`anymodbus`](https://pypi.org/project/anymodbus/).

The ZPA is also sold as the **CAI ZPA** by California Analytical Instruments
(CAI), now part of **ENVEA**. `fujilib` is developed and tested against a
CAI-branded ZPA. See [Also sold as](#also-sold-as).

!!! warning "Alpha"
    0.2.0 is the current release. The read-only API works against the development
    analyzer: `open_device()`, identification, polls with validity, metadata,
    settings, logs, discovery, [recording](recording.md) to memory, CSV or
    Parquet, a blocking facade and the [`fuji-*` commands](cli.md). A reviewed
    subset of settings writes, settings documents, return to measurement, and
    manual zeros and spans watched at the panel or driven from the host
    (`fuji-calibrate`) work on the development analyzer too; auto calibration
    and auto zero have run only on the simulated analyzer, since the
    development analyzer has no calibration valves. See [Safety](safety.md).
    Start with the [async quickstart](quickstart-async.md).

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

## Also sold as

ZP-series analyzers are sold under other names too:

| Sold as | By | Status |
|---|---|---|
| ZPA NDIR/O2 Multichannel Analyzer (CAI ZPA) | California Analytical Instruments (CAI), now part of ENVEA (CAI ENVEA Group) | The development analyzer is a CAI-branded ZPA. |
| IR202 Infrared Gas Analyzer | Yokogawa | Untested. Its MODBUS manual (IM 11G02Q02-51EN) documents the same register map and the same 38400 8-N-1 link. |

`fujilib` is an independent project. It is not affiliated with or endorsed by
Fuji Electric, California Analytical Instruments, ENVEA or Yokogawa; their names
and product names belong to their owners.
