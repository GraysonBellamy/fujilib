---
description: The procedure for running fujilib's tests against a real Fuji ZP-series analyzer.
---

# Hardware test day

> The written procedure for the hardware tests (design §10). Each tier needs the
> owner's authorization before it is ever run; this page covers the read-only
> tier, the only one that exists so far. Results go to
> [protocol-findings.md](protocol-findings.md).

## Bench facts

| | |
|---|---|
| Analyzer | ZPA, serial `N8A0259T`, program version 1.02 |
| Channels | Ch1 CO2 0-10 vol%, Ch2 CO 0-1 vol%, Ch3 O2 0-21 / 0-25 vol% |
| Link | RS-485 through an FTDI FT232R adapter; `COM8` when last checked |
| Station | 1 |
| Framing | 38400 8-N-1, fixed |
| Firmware features | clock and A/D block present; no calibration log and no type-code digits 27-29 |

COM port numbers can change when adapters are replugged. When in doubt, find
the port by unplugging and replugging the analyzer's adapter while listing the
ports. That sends nothing to the other instruments on the rig.

## Scope

| Allowed | Not allowed |
|---|---|
| Every read the library makes: identity, polls, status, metadata, settings, logs, clock, A/D | Any register write |
| Discovery on the analyzer's own port, its station and the next one | Operation commands: auto calibration, auto zero, blowback, return to measurement |
| The command-line tools, which only read | Key simulation (never, design §6.5) |
| | Probing ports that belong to other instruments |

## How the bench is driven on the development machine

Serial work on the development machine has been run from Git Bash, with `uv`
on the path:

```bash
cd fujilib
export PATH="$HOME/.local/bin:$PATH"
FUJILIB_ENABLE_HARDWARE_TESTS=1 FUJILIB_HARDWARE_PORT=COM8 \
    uv run pytest -m hardware tests/hardware -v
```

`FUJILIB_HARDWARE_ADDRESS` sets the station when it is not 1. The tests run
under asyncio and trio.

## Pre-flight checklist

1. The analyzer is powered on and shows the measurement screen.
2. No one is working at the analyzer's front panel. The panel stays live, and
   a range changed there shows up in the tests.
3. Nothing else has the port open: no terminal program, no other session.
4. The port is the analyzer's (see *Bench facts*).

## The read-only session

`tests/hardware/` holds three files:

| File | Covers |
|---|---|
| `test_hardware_client.py` | the transport, the Modbus client and the read procedures: identity, poll timing after the inter-frame gap, status, ranges, metadata, settings, logs, clock and A/D, 50 sustained polls, and a cancelled read that must not disturb the next |
| `test_hardware_reads.py` | the `Analyzer` facade: open and identify, an asserted label, a poll of exactly two transactions, polls without detail, status, metadata, ranges, settings and parameters, the logs (the calibration log refused before any I/O on firmware 1.02), clock, A/D and reprobing, 50 sustained polls, a deadline, an empty station that times out and releases the port, and closing and opening again |
| `test_hardware_sync.py` | the blocking facade, discovery on the analyzer's port, and `fuji-read`, `fuji-discover` and `fuji-configure dump` |

Every assertion holds for any ZP analyzer; values particular to the bench unit
are recorded in the findings instead.

## Deliverables

- The test run's summary, with any failures and whether they reproduce.
- New findings, dated, in [protocol-findings.md](protocol-findings.md), and the
  design sections they change.
- Captures, if any were made, kept in `tests/fixtures/captures/` (git-ignored).

## Risks

- **Background loss.** About one request in 3,000 goes unanswered (findings
  §6.3). A read retries, so a test that counts attempts can fail on it; rerun
  that test on its own before calling it a fault.
- **Port release on Windows.** Closing and reopening the same port at once has
  worked on the bench; if it ever fails with "access is denied", wait briefly
  and retry.
- **The operator.** A front-panel change during the run (range, hold,
  calibration) changes what the tests read.
