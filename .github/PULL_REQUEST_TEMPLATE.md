## Summary

<!-- What changes and why. Link to the design section it realises, if any ("design §N"). -->

## Scope

- [ ] Touches a public API surface
- [ ] Touches the transport or Modbus layer (`transport/`, `protocol/modbus/`)
- [ ] Adds or changes a `RegisterSpec` in the registry (regenerate `docs/registers.md`)
- [ ] Changes `WRITE_ENVELOPE`, an `OperationSpec`, a safety tier or a capability gate
- [ ] Changes the `Sample` / row shape that `capa` consumes

## Test plan

- [ ] `uv run pytest` green locally
- [ ] `uv run ruff format --check .` and `uv run ruff check .` clean
- [ ] `uv run mypy` and `uv run pyright` clean (no new ignores)
- [ ] New behaviour has a test against `MockAnalyzer` or a fixture (no hardware required)
- [ ] Hardware-only tests marked (`hardware`, `hardware_stateful`, `hardware_destructive`)

## Safety checklist (anything that writes)

- [ ] No write can be constructed outside `WRITE_ENVELOPE`; nothing writes register 42001 (key simulation)
- [ ] Operations above `READ_ONLY` require `confirm=True` before any I/O
- [ ] Values are validated before I/O, and scaled values re-read their scaling first
- [ ] Writes are verified by read-back, never retried, and report an unknown outcome as unknown
- [ ] Registers the manual does not document stay read-only
