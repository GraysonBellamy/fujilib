# Security policy

## Reporting a vulnerability

Please email [gbellamy@umd.edu](mailto:gbellamy@umd.edu) or open a
private security advisory on GitHub:
<https://github.com/GraysonBellamy/fujilib/security/advisories/new>.

Do **not** file public issues for security reports.

## Scope

`fujilib` drives gas analyzers over serial. Please report:

- Any path that sends a write outside `WRITE_ENVELOPE`, or any write to the
  key-simulation register (42001), which can reach the analyzer's factory menu.
- Code paths that send `STATEFUL`, `PERSISTENT` or `DANGEROUS` operations
  without `confirm=True`, including through the CLI or a settings file.
- Writes or operation commands that run as a side effect of
  `open_device(...)`, `identify()`, discovery or recording.
- Deserialisation of untrusted input in fixture loaders or settings files.
- Any path that logs credentials, DSNs or secrets held by a sink.
