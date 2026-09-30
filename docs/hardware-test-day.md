---
description: The procedure for running fujilib's tests against a real Fuji ZP-series analyzer.
---

# Hardware test day

> The written procedure for the hardware tests (design §10). Each tier needs the
> owner's authorization before it is ever run. This page covers:
>
> - the read-only tier;
> - the stateful tier, which writes settings and restores them;
> - the watching of a calibration the owner makes at the front panel;
> - the key prototype, which presses front-panel keys over Modbus without
>   calibrating.
>
> There are no destructive tests: the bench analyzer cannot auto-calibrate.
> Results go to [protocol-findings.md](protocol-findings.md).

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
| The command-line tools, which only read | Key simulation (design §6.5, Phase 7B) |
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
| `scripts/probe_write.py`, step by step | Key simulation (design §6.5, Phase 7B) |
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

## Watching a calibration at the panel

fujilib only reads here. The owner calibrates at the front panel, with the
gases at the inlet, and decides each step; it needs the owner's authorization
like any session, because it changes the analyzer's calibration. It checks
that every manual zero and span is recorded as it happened (design §6.5,
Phase 7A).

1. **Pre-flight** as for the read-only session. Note the calibration gas
   settings of the channel to be calibrated (`fuji-configure dump`), and
   whether it is set to zero "at once" or calibrate "both" ranges:
   `plan_manual_calibration()` lists what a zero or span would touch.
2. **Start the watcher**, and leave it running:

   ```bash
   uv run python examples/watch_manual_calibration.py COM8
   ```

3. **A zero**, with zero gas flowing: ZERO, the cursor to the channel, ENT to
   select it, and ENT again once the reading is steady. Expected: one line
   with `"outcome": "completed"`, the channel, the reading before, `after` at
   the zero gas, and the deviation.
4. **A span**, with span gas flowing: SPAN, the channel, ENT, ENT. Expected
   as for the zero, with `after` at the span gas setting.
5. **A cancel**: ZERO, the channel, ENT, then ESC. Nothing is calibrated.
   Expected: `"outcome": "cancelled"`.
6. **Stop the watcher** with Ctrl-C.

The 2026-09-29 sessions recorded the same sequence with
`scripts/probe_calibration.py` (findings §14) and then with this watcher, which
reported the zero and span completed and the cancel cancelled (findings §16). The
key prototype below answered which flags the "at once" pair sets (findings §18.5).
A channel set to "both", output hold and a calibration that fails are still open
(design §13.2 #36-#38).

## The key prototype

This session finds out what a key written to 42001 does before fujilib drives
a manual calibration itself (design §6.5, §12 Phase 7B).
`scripts/probe_panel.py` presses front-panel keys over Modbus, one experiment
per run. It needs the owner's authorization for the session, with the owner at
the front panel throughout (design §13.1 #79). **Nothing in it calibrates.**

| Allowed | Not allowed |
|---|---|
| `scripts/probe_panel.py`, one experiment at a time | MODE and SIDE, which open the menus and enter their passwords; two keys at once |
| ZERO, SPAN, UP, DOWN and ESC; ENT on channel selection, where it selects the channel | ENT on a wait step, where it starts the calibration, or on the error display, where it can force one (ZPA p.89) |
| 42002, return to measurement | 42003-42005: auto calibration, auto zero, blowback |
| Output hold switched on for the hold step and off after it, with `fuji-configure apply` | Any calibration. The error display and a channel set to "both" need one (design §13.2 #36, #37), so they are left out |
| | Any other register write |

The probe enforces this itself:

- Its one write takes an address and a value from a fixed list: the six keys to
  42001, and 1 to 42002.
- It reads the panel just before every key, and refuses a key the step does not
  allow. ENT also needs the cursor on the channel the experiment names.
- A key is sent once and never repeated. A lost reply is settled by reading the
  panel.
- After each key it reads the step, the cursor, the zero, span and hold flags,
  00B9h and 00BDh until they settle, and records when each changed.
- Anything unexpected ends the experiment, and a cleanup follows:
  - ESC on channel selection, a wait step or the error display;
  - a wait while a calibration runs;
  - 42002 on any other screen.

  It then checks the flags, not only the screen. It sends each of its keys
  once, and not at all if the experiment has just sent the same key on the same
  step.

**At the panel:**

- Keep your hands off the keys while an experiment runs. The probe reads the
  step just before each key, so a key pressed at the panel between that read
  and the write lands on a step it did not check.
- If it is practical, have zero gas at the inlet for the zero experiments and
  span gas for the span ones. Then even a calibration started by mistake would
  be made on the right gas. The probe never sends the ENT that calibrates, so
  this is a second line of defence, not a requirement.
- Say what the display shows where the probe cannot see it: the backlight, a
  key-lock message.

### Before the first key

1. Pre-flight as for the read-only session. Check the process list: no
   recording, soak monitor or probe has `COM8` open.
2. Check the probe on the simulator. It opens no port and must end with
   `every check passed`:

   ```bash
   uv run python scripts/probe_panel.py check
   ```

3. Save the settings:

   ```bash
   fuji-configure dump COM8 --out probe_out/settings_before_keys.json
   ```

4. Read the panel and the settings the experiments depend on. This reads only:

   ```bash
   uv run python scripts/probe_panel.py --port COM8 status
   ```

   Expected:
   - the measurement screen, step 0, no calibration or hold flag set;
   - key lock off and output hold off;
   - Ch3 zeroed on its own ("each"), and Ch1 and Ch2 zeroed "at once".

### The experiments

Run each in this order, one at a time:

```bash
uv run python scripts/probe_panel.py --port COM8 <experiment> --confirm
```

| # | Experiment | Keys sent | Expected | What it answers |
|---|---|---|---|---|
| 1 | `select-esc` | ZERO, ESC; SPAN, ESC | step 0 → 4 → 0, then 0 → 7 → 0; no flag set | whether a key acts, whether it shows in 00BDh, how soon the step follows; ESC from channel selection |
| 2 | `cursor` | ZERO, DOWN ×3, UP ×3, ESC; then the same after SPAN | a zero offers the Ch1 position (Ch1 and Ch2 "at once") and Ch3; a span offers Ch1, Ch2 and Ch3 | whether the cursor stops at the ends or wraps round |
| 3 | `zero-cancel` | ZERO, UP or DOWN to Ch3, ENT, ESC | step 5 with Ch3's zero flag only, 00B9h at 0; ESC gives step 0 with the flag clear | the channel-selecting ENT, and ESC from the wait step |
| 4 | `span-cancel` | SPAN, to Ch3, ENT, ESC | step 8 with Ch3's span flag only; ESC gives step 0 with the flag clear | the same for a span |
| 5 | `return-select` | ZERO, 42002 | not known | what 42002 does on channel selection |
| 6 | `return-wait` | ZERO, to Ch3, ENT, 42002 | the measurement screen with **Ch3's zero flag left set** (findings §18.4); clear it at the panel: ZERO, O2, one ENT, ESC | what 42002 does on a wait step, and whether the flag clears with the step |
| 7 | `at-once` | ZERO, UP to the Ch1 position, ENT, ESC | step 5 with the zero flags of Ch1 and Ch2, perhaps also of the absent Ch4 and Ch5; ESC clears them all | design §13.2 #37, the "at once" pair |
| 8 | `key-lock` | ZERO, then ESC if it opened channel selection | not known | whether key lock stops a key written over Modbus |
| 9 | `backlight` | ZERO, then ESC if it opened channel selection | not known | whether a key is swallowed while the backlight is off |
| 10 | `hold` | ZERO, to Ch3, ENT; 120 s on the wait step; ESC | see below | design §13.2 #38 |

**Key lock (8).** Switch key lock on at the front panel (Parameter Setting),
return to the measurement screen, and run the step. Say what the display does.
Switch key lock off afterwards.

**Backlight (9).** Let the backlight go off. Its timer is set in Parameter
Setting, 1-60 minutes; set it as you prefer, and put it back afterwards. Run
the step without touching the panel, and say whether the backlight came on.

**Output hold (10).** Switch output hold on with a one-setting document, then
run the step in the background: it takes up to 10 minutes.

```bash
fuji-configure apply COM8 --file probe_out/output_hold_on.json --confirm
uv run python scripts/probe_panel.py --port COM8 hold --confirm
```

`output_hold_on.json` is
`{"format": "fujilib-settings/1", "settings": {"output_hold.enabled": true}}`.

1. Start with span gas (air) at the inlet.
2. When the panel shows the zero wait screen for Ch3, switch to zero gas. The
   probe reads the readings and the A/D values for 120 s (`--dwell`), then
   sends ESC.
3. After ESC it reads on until the hold flag clears, for at most 7 minutes
   (`--hold-watch`). After a calibration is cancelled, the analyzer holds the
   outputs for the hold extension (ZPA p.66 e.), and this unit's gas flow times
   are all 300 s.
4. Keep the zero gas until the probe ends, then switch back to air.
5. Switch output hold off with the same document set to `false`.

Expected (ZPA p.67): the Modbus readings stay at their value from before the
hold, while the O2 A/D count follows the gas. That would let the steadiness
rule use the counts where hold is on (design §13.1 #78). To end the watching
early, create `probe_out/probe_panel.stop`: the probe sends the ESC and stops.

### Closing

Compare with the saved settings; nothing may be written. Then read the panel:

```bash
fuji-configure diff COM8 --file probe_out/settings_before_keys.json
uv run python scripts/probe_panel.py --port COM8 status
```

Every setting must be unchanged, and the panel on measurement with no flag set.

### When something is unexpected

- An experiment that ends `unexpected`, or a cleanup that ends `NOT CLEAN`,
  stops the session. Look at the panel. Run nothing more until `status` shows
  the measurement screen with no flag set.
- If a flag stays set on the measurement screen, the probe sends nothing more,
  and the owner decides at the panel. As a last resort, switching the analyzer
  off and on clears the panel's state (findings §15.5); the readings are wrong
  for about a minute afterwards (design §13.1 #81).

**Not in this session:**

- the error display (design §13.2 #36) and a channel set to "both" (#37). Both
  need a real calibration, and one that fails can change the calibration. They
  need the owner's explicit acceptance, in a session of their own;
- anything touching auto calibration, auto zero or blowback, which are never
  run on this unit.

Each run writes `probe_out/probe_panel_<experiment>_<time>.json`. The results
go to protocol findings §18.

**The session of 2026-09-30** (12:56-13:12 UTC, findings §18) ran experiments 1-8.
`span-cancel` came after `at-once`, so the gas changed only once. The backlight and
output-hold steps were not run. Every key acted as at the panel, and nothing was
calibrated. At the close all 162 settings matched the saved ones. Only
`return-wait` did not end clean: its 42002 left Ch3's zero flag set, and the owner
cleared it at the panel.

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
