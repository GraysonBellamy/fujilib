---
description: The procedure for running fujilib's tests against a real Fuji ZP-series analyzer.
---

# Hardware test day

> The written procedure for the hardware tests (design §10). Each tier needs the
> owner's authorization before it is ever run; this page covers the read-only
> tier and the stateful one, which writes settings and restores them. There
> are no destructive tests: the bench analyzer cannot auto-calibrate. Results
> go to [protocol-findings.md](protocol-findings.md).

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

## Scope of the read-only session

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

`tests/hardware/` holds four files:

| File | Covers |
|---|---|
| `test_hardware_client.py` | the transport, the Modbus client and the read procedures: identity, poll timing after the inter-frame gap, status, ranges, metadata, settings, logs, clock and A/D, 50 sustained polls, and a cancelled read that must not disturb the next |
| `test_hardware_reads.py` | the `Analyzer` facade: open and identify, an asserted label, a poll of exactly two transactions, polls without detail, status, metadata, ranges, settings and parameters, the logs (the calibration log refused before any I/O on firmware 1.02), clock, A/D and reprobing, 50 sustained polls, a deadline, an empty station that times out and releases the port, and closing and opening again |
| `test_hardware_sync.py` | the blocking facade, discovery on the analyzer's port, and `fuji-read`, `fuji-discover` and `fuji-configure dump` |
| `test_hardware_recording.py` | a 5 Hz recording (schedule, fixed row columns), `pipe()` to CSV and Parquet, `fuji-stream`, `fuji-capture`, and a short `fuji-diag timing` run |

Every assertion holds for any ZP analyzer; values particular to the bench unit
are recorded in the findings instead.

## The 12-hour recording

The hardware exit of Phase 5 (design §12, §13.1 #58). It is read-only, and it holds
the port for 12 hours; overnight suits it.

1. Pre-flight as above, plus: the host does not sleep, USB selective suspend is
   off for the adapter, and no restart for updates is due.
2. Open a console window yourself (Command Prompt, PowerShell or Windows
   Terminal), go to the repository, and start it on one line:

   ```
   uv run --with psutil python scripts/soak_monitor.py --log probe_out/soak.rss.jsonl -- fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 --rate 1 --duration 43200 --out probe_out/soak.parquet --reconnect
   ```

   A progress line every 10 s shows the polls, failures and late ticks so far;
   the memory log gets a line every 10 minutes, and `probe_out/soak.parquet.meta.json`
   gets the counters so far every minute. Selecting text in the window pauses
   the progress line until the selection ends, but not the recording.
3. Leave it alone. A front-panel change is recorded, not an error.
4. To stop it early, press Ctrl-C; if Ctrl-C does nothing, press Ctrl-Break.
   Either stops it cleanly: wait for its summary before closing the window.
   **Never close the window while it records**: that kills it, and a killed
   Parquet file has no footer and cannot be read.
5. When it ends, check it:

   ```
   uv run python scripts/check_soak.py probe_out/soak.parquet --rss probe_out/soak.rss.jsonl
   ```

   It checks tick and row counts, timing, error accounting, status and
   provenance in every row, a clean shutdown, readable output and bounded
   memory (the trend of private memory after the first hour), and writes
   `probe_out/soak.parquet.check.json`.
6. If it was killed anyway, recover the complete row groups (1,000 rows each)
   into `probe_out/soak.recovered.parquet`, with a copy of the `.meta.json`
   beside it, and check that file instead. It fails the shutdown check, and
   the counts it cannot judge without final counters are marked SKIP:

   ```
   uv run python scripts/recover_parquet.py probe_out/soak.parquet
   uv run python scripts/check_soak.py probe_out/soak.recovered.parquet --rss probe_out/soak.rss.jsonl
   ```

## The unplug test

A controlled disconnect and reconnect (design §12). Someone must be at the
bench to pull the adapter's USB plug. Read-only.

1. Start a 10-minute capture with reconnection:

   ```bash
   fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 \
       --rate 1 --duration 600 --out probe_out/unplug.csv --reconnect
   ```

2. After about two minutes, pull the adapter's USB plug; put it back after
   about 30 s. Repeat once. The progress line counts the failed polls meanwhile.
3. Expected: the capture finishes with exit code 0; the rows of each outage are
   error rows (`FujiConnectionError`, then refusals until the port is back);
   `probe_out/unplug.csv.meta.json` reports 2 disconnects and 2 reconnects.
   Check that the port came back as `COM8`.
4. Run it again without `--reconnect`: at the first pull it ends with exit
   code 1, and the CSV and its `.meta.json` (state `failed`) are complete up to
   the failure, with 1 disconnect and 0 reconnects.

## The stateful session

It writes settings, reads each back and restores it, and needs its own
authorization and the owner at the front panel. Nothing in it starts a
calibration: the bench analyzer's type code lists no auto-calibration option,
and the analyzer has no calibration valves of its own, so auto calibration
would calibrate on whatever gas is at the inlet ([Safety](safety.md)).

| Allowed | Not allowed |
|---|---|
| Writes of the reviewed settings, each restored | Auto calibration, auto zero calibration (never on this analyzer) |
| Return to measurement | Blowback (not a ZPA feature) |
| `scripts/probe_write.py`, step by step | Key simulation (never, design §6.5) |
| | Any register outside the reviewed subset |

1. **Pre-flight** as for the read-only session, and save the settings first:

   ```bash
   fuji-configure dump COM8 --out probe_out/settings_before.json
   ```

2. **The stateful tests.** The first ones write only registers of channels and
   NDIR components the bench analyzer does not have (Ch4, Ch5, NDIR 4), with
   both FC06 and FC10; then the O2 response time, output hold, hold mode and
   Ch3's range, each restored; then a settings document and its baseline. A
   fixture reads every writable setting before each test and checks after it
   that the analyzer has them all back.

   ```bash
   FUJILIB_ENABLE_STATEFUL_TESTS=1 FUJILIB_HARDWARE_PORT=COM8 \
       uv run pytest -m hardware_stateful tests/hardware -v
   ```

3. **Key lock** (design §13.2 #12). Switch key lock on at the front panel,
   then run the step. It writes one register of Ch5 and sends return to
   measurement, records whether each was applied, ignored or refused, and
   restores the register. Switch key lock off at the panel afterwards.

   ```bash
   uv run python scripts/probe_write.py --port COM8 key-lock --confirm
   ```

4. **A menu at the panel.** Open any menu at the front panel, then try a write
   (`fuji-configure apply` of a one-setting document): it must be refused with
   nothing written. Then `return_to_measurement(confirm=True)` must bring the
   panel back to the measurement screen.
5. **Power cycle** (design §13.2 #14). Write a marker, switch the analyzer off
   and on, wait for the measurement screen, and check it; the second step
   restores the register.

   ```bash
   uv run python scripts/probe_write.py --port COM8 persist-write --confirm
   # switch the analyzer off, then on
   uv run python scripts/probe_write.py --port COM8 persist-check --confirm
   ```

6. **Values out of range**, if authorized on the day. It writes 61, then 0,
   to the response time of NDIR component 4 through the client, past the
   library's own limit, records whether the analyzer refuses, clamps or stores
   each, and restores it.

   ```bash
   uv run python scripts/probe_write.py --port COM8 out-of-range --confirm
   ```

7. **The start time of the schedules** (design §13.2 #26): read the
   auto-calibration start time the front panel shows. No Modbus is needed.
8. **Close**: compare with the saved settings; nothing may be written.

   ```bash
   fuji-configure diff COM8 --file probe_out/settings_before.json
   ```

   Every setting must be unchanged. If one is not, apply the file with
   `fuji-configure apply ... --confirm` (and the destructive flag for a
   calibration gas).

Stop at the first unexpected result and record it: a write that reads back
otherwise, an unknown outcome, or a setting left changed.

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
