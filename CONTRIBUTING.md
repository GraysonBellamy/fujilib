# Contributing to fujilib

Thanks for your interest. Please read [docs/design.md](docs/design.md)
before making non-trivial changes — most design decisions are already
made and documented there. Code comments cite it as "design §N".

## Dev setup

```bash
git clone https://github.com/GraysonBellamy/fujilib
cd fujilib
uv sync --all-extras --dev
uv run pre-commit install
```

## Core checks (must pass before merging)

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pyright
uv run pytest
```

## Adding a register

The register map is a typed Python registry, not a data file (design §5.1).
The registry lands in Phase 1 (design §12); until then this section describes
the planned workflow. To add or correct a register:

1. Declare its `RegisterSpec` in `src/fujilib/registry/registers.py`, with the
   table, 0-based address, word count, data type, access, the function codes
   it may be read and written with, scaling and unit rules, raw limits, and a
   `manual_ref` pointing at the manual page.
2. Mark registers the manual does not document with `evidence=OBSERVED` or
   `INFERRED`. They are never writable.
3. Regenerate the register reference with
   `uv run python scripts/gen_register_docs.py`; CI fails if
   `docs/registers.md` is stale.
4. Add a test that decodes a fixture containing the register.

The registry validates itself at import: no overlaps, every spec inside a valid
region for each function code it lists, and every writable spec inside the
write envelope.

## Safety

What fujilib may ever write is the frozen `WRITE_ENVELOPE` in
`src/fujilib/registry/write_policy.py` (design §5.4). It is never derived from
the registry or from probing, and widening it needs a design change first.
fujilib never writes the key-simulation register (42001) and never writes
factory parameters (design §6.5).

Anything above `SafetyTier.READ_ONLY` must accept `confirm=True`, and the
session rejects the call before any I/O without it (design §6.1). Writes are
verified by reading back and are never retried automatically (design §6.4).

## Commits

Conventional-style short prefixes are helpful but not mandatory:

- `feat:` new user-visible behaviour
- `fix:` bugfix
- `refactor:` internal cleanup
- `docs:` docs only
- `ci:` pipeline changes
- `chore:` tooling/version bumps

## Tests that need hardware

Mark them with `hardware`, `hardware_stateful`, or `hardware_destructive`.
They are deselected by default and also skipped unless their opt-in variable is
set (`FUJILIB_ENABLE_HARDWARE_TESTS=1`, `FUJILIB_ENABLE_STATEFUL_TESTS=1`,
`FUJILIB_ENABLE_DESTRUCTIVE_TESTS=1`). The bench port and station come from
`FUJILIB_HARDWARE_PORT` and `FUJILIB_HARDWARE_ADDRESS`. Stateful and
destructive tests change the analyzer's settings or calibration; run them only
with the owner's authorization for that session.
