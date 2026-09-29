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

**Pre-alpha, unreleased.** The read-only API works against the development
analyzer: `open_device()`, identification, polls with validity, metadata,
settings, logs, discovery, recording at a fixed rate to memory, CSV or Parquet,
a blocking facade, and the `fuji-read`, `fuji-discover`, `fuji-configure`,
`fuji-decode`, `fuji-stream`, `fuji-capture` and `fuji-diag` commands. A
reviewed subset of settings writes, settings documents and return to
measurement work on the development analyzer too; auto calibration and auto
zero have run only on the simulated analyzer, since the development analyzer
has no calibration valves. See
[`docs/design.md`](docs/design.md) for the architecture and the phased plan, and
[`CHANGELOG.md`](CHANGELOG.md) for what has landed.

```python
import anyio

from fujilib import open_device


async def main() -> None:
    async with await open_device(
        "COM8", channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}
    ) as anz:
        frame = await anz.poll()  # every channel and the analyzer status, 2 transactions
        o2 = frame.channel("CH3")
        print(o2.value, o2.unit, o2.state)  # 20.2 vol% ok


anyio.run(main)
```

Record to a file from the command line (Parquet needs `fujilib[parquet]`):

```bash
fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 --out run.parquet
```

The first release, **0.1.0**, covers:

- monitoring, metadata and acquisition. `poll()` returns every channel with
  its hold, calibration and error state in two Modbus transactions, and the
  library streams and records to memory, CSV or Parquet;
- a reviewed subset of settings writes and the documented operation commands
  (auto calibration, auto zero, blowback, return to measurement), behind
  `confirm=True`, pre-I/O validation and read-back verification.

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
