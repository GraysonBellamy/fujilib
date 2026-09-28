---
description: Installing fujilib from source until the first release.
---

# Installation

fujilib is not on PyPI yet. Requires Python 3.13+.

Install from source with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/GraysonBellamy/fujilib
cd fujilib
uv sync --all-extras --dev
```

The core depends on `anyio`, `anyserial` and `anymodbus`. MODBUS RTU is the
analyzer's only protocol, so `anymodbus` is a core dependency rather than an
extra.
