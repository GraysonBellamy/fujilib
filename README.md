# fujilib

> Async-first Python driver for **Fuji Electric ZP-series NDIR gas analyzers**
> (ZPA, and the ZPB / ZPG / ZPAJ / ZPG3E models that share its MODBUS map),
> built on [`anyserial`](https://pypi.org/project/anyserial/) and
> [`anymodbus`](https://pypi.org/project/anymodbus/).

`fujilib` is a member of the `*lib` instrument-driver family (`alicatlib`,
`sartoriuslib`, `watlowlib`, `servomexlib`, …). It shares their entry point,
frozen models, error hierarchy, streaming / sinks / sync / CLI conventions and
the unified device-library API; its internals are shaped to this analyzer.

## Status

**Pre-alpha. Nothing usable yet.** The repository holds the design, the
read-only bench findings, the project scaffolding and the error hierarchy.
See [`docs/design.md`](docs/design.md) for the architecture and the phased
plan, and [`CHANGELOG.md`](CHANGELOG.md) for what has landed.

Planned releases:

- **0.1.0**: read-only monitoring, metadata and acquisition. `poll()` returns
  every channel with its hold, calibration and error state in two Modbus
  transactions, and the library streams and records to memory, CSV or Parquet.
- **0.2.0**: a reviewed subset of settings writes and the documented operation
  commands (auto calibration, auto zero, blowback, return to measurement),
  behind `confirm=True`, pre-I/O validation and read-back verification.

## Design points

- **Validity and provenance travel with every value.** Hold, calibration and
  error state, and where each channel's gas label came from, are carried into
  every reading and row. Unknown validity stays unknown.
- **Gas labels that feed a calculation are asserted by the caller.** The
  analyzer's type code is treated as a hint, not an authority.
- **Writes are hard to get wrong.** Everything fujilib may ever write is a
  frozen envelope of documented user settings and operation commands. Factory
  parameters, key simulation and manual calibration are out of scope.
- **No hardware needed to develop or test.** A simulated analyzer runs the full
  stack in CI.

## Oxygen measurements

Over Modbus the analyzer reports O2 with the display's resolution: **0.01 vol%
(100 ppm) per step** on the development unit. Modbus O2 is **not validated for
oxygen-consumption calorimetry**. See design §2.11.

## Installation

Not on PyPI yet. Requires Python 3.13+. From source:

```bash
git clone https://github.com/GraysonBellamy/fujilib
cd fujilib
uv sync --all-extras --dev
```

## Development

```bash
uv run pre-commit install
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pyright
uv run pytest
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT. See [`LICENSE`](LICENSE).
