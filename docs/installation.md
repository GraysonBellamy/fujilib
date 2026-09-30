---
description: Installing fujilib from PyPI or from source.
---

# Installation

Requires Python 3.13+.

```bash
pip install fujilib
```

or, with [uv](https://docs.astral.sh/uv/):

```bash
uv add fujilib
```

The Parquet sink needs the `parquet` extra (`pyarrow`):

```bash
pip install "fujilib[parquet]"
```

The core depends on `anyio`, `anyserial` and `anymodbus`. MODBUS RTU is the
analyzer's only protocol, so `anymodbus` is a core dependency rather than an
extra.

## From source

```bash
git clone https://github.com/GraysonBellamy/fujilib
cd fujilib
uv sync --all-extras --dev
```
