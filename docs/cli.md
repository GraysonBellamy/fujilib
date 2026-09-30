---
description: The fuji-* command-line tools — read, discover, decode, compare and apply settings, stream, capture and diagnose a Fuji ZP-series analyzer.
---

# Commands

Every command but `fuji-configure apply` and `fuji-calibrate` is read-only: it
sends read requests and nothing else. `fuji-configure apply` writes settings,
and `fuji-calibrate` presses the calibration keys, each behind `--confirm`
(see [Safety](safety.md)). Each command is also a function,
`main(argv) -> int`, in `fujilib.cli`.

| Command | What it does |
|---|---|
| `fuji-read` | read an analyzer once: identity, a poll, status, metadata, ranges, logs, clock, A/D, snapshot |
| `fuji-discover` | find ZP analyzers on named ports (or every port, with `--all-ports`) |
| `fuji-decode` | decode MODBUS frames or a register dump, offline |
| `fuji-configure dump` | read every setting and write it as a `fujilib-settings/1` JSON document |
| `fuji-configure diff` | compare a settings document with the analyzer |
| `fuji-configure apply` | write the settings of a document that differ, each read back |
| `fuji-stream` | poll at a fixed rate and print each poll |
| `fuji-capture` | record to a CSV or Parquet file, with the analyzer's metadata beside it |
| `fuji-diag timing` | measure the gap the analyzer needs between requests |
| `fuji-calibrate` | make a manual zero or span from the host, the operator at the gas valves |

**Exit codes.** 0 on success, 1 for a library error (printed as `error: ...`),
2 for bad arguments. `fuji-discover` also exits 2 when it finds nothing;
`fuji-configure apply` exits 1 when the document is refused or a write fails,
and 2 when a write would be DANGEROUS without its flag. `fuji-calibrate`
exits 0 only when the calibration completed (or with `--plan`), and 1 when it
was refused, cancelled, failed or ambiguous.
`fuji-stream` and `fuji-capture` exit 0 when stopped with Ctrl-C, after
writing and closing what they recorded. On Windows, Ctrl-Break stops them (and
`fuji-diag timing`) the same way, and it works in a window whose processes
ignore Ctrl-C, as they do in a window opened by a process that ignores it.
Closing the window instead kills the command.

**Which analyzer.** The commands that talk to one analyzer take a serial port
(`COM8`, `/dev/ttyUSB0`) and `--address` (the station, 1 by default), or
`--fixture BANK` to run against a simulated analyzer answering from a register
bank; `--fixture bench` is the bank bundled with fujilib. `--gas CHn=gas`
asserts a channel's gas (repeatable), which is the only way to give a
calculation a trustworthy label (design §2.9).

## fuji-read

```
fuji-read COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2
fuji-read COM8 --include metadata --include error-log --format json
fuji-read --fixture bench --all
```

`--include` chooses sections (identity and one poll by default); a section
the analyzer does not support, such as the calibration log before firmware
2.24, is reported as unavailable.

## fuji-discover

```
fuji-discover COM8 COM4 --addresses 1-5
fuji-discover --all-ports
```

Every port scanned receives the probe frames, including ports of other
instruments, so the host's ports are scanned only with `--all-ports`.

## fuji-configure

```
fuji-configure dump COM8 --out zpa-settings.json
fuji-configure diff COM8 --file changes.json
fuji-configure apply COM8 --file changes.json --confirm
fuji-configure apply COM8 --file gases.json --confirm --i-understand-this-is-destructive
```

`dump` writes every holding register by name, with its decoded value, raw
word, unit, access, safety tier and evidence, and the analyzer's identity, as
a `fujilib-settings/1` document. `--alarm-target N=CHn` scales alarm N's
limits by that channel's range.

`diff` reads the analyzer and says, for every setting the document names,
whether it would be written (with its tier), is refused (with the reason), or
is unchanged. A document written by hand may name only the settings to change:

```json
{
  "format": "fujilib-settings/1",
  "settings": {
    "response_time.o2": 20,
    "hold.mode": "setting",
    "calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"}
  }
}
```

A calibration gas needs its unit, which must be its range's. An enumerated
setting takes its name (`setting`, `range_2`, `auto`), never its number.

`apply` compares first, and writes nothing if anything is refused or the
document is another analyzer's (`--any-analyzer` accepts it). Then it writes
each setting that differs, in dependency order, reads it back, and stops at
the first that fails. It ends with `status:` `ok`, `dry_run`, `refused`,
`partial`, `verify_failed`, `unknown` or `failed`, and a `recovery:` hint when
something went wrong. `--dry-run` stops after the comparison. See
[Safety](safety.md) for what may be written and why.

## fuji-stream

```
fuji-stream COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2
fuji-stream COM8 --rate 2 --duration 60 --format csv > run.csv
```

One line per poll on standard output, as readable text (the default), CSV or
JSON lines; the recording's counters go to standard error at the end. It runs
until `--duration` has passed, or Ctrl-C.

## fuji-capture

```
fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 --out run.parquet
fuji-capture COM8 --out run.csv --rate 1 --duration 86400 --reconnect
```

It identifies the analyzer and reads its metadata, then records at `--rate`
until `--duration` has passed or Ctrl-C, printing a progress line every 10 s
on standard error (`--quiet` stops it). Two files are written:

- **`--out`**: one row per poll. The format follows the extension (`.csv`,
  `.parquet`) unless `--format` says otherwise. Parquet needs the `parquet`
  extra, which is checked before the port is opened.
- **`<out>.meta.json`** (format `fujilib-capture/1`): the analyzer's identity,
  ranges and metadata (response times, calibration gases, hold mode, clock),
  the arguments, the package versions, and, when the recording ends, how it
  ended (`finished`, `stopped` or `failed`, with the error) and its counters.
  While it records, it is rewritten every minute with the counters so far
  (`state` stays `recording`), so a capture that is killed still says how far
  it got; `updated_at` says when it was last written. A Parquet file also
  carries the starting version in its metadata, as `fujilib.capture`.

Existing files are not replaced without `--force`; a missing directory for
`--out` is created. A connection failure ends
the capture with exit code 1 unless `--reconnect` asks for the port to be
reopened; either way the files are complete up to that point. For an
unattended recording, CSV is the safer format: a Parquet file is readable only
once it is closed. The repository's `scripts/recover_parquet.py` recovers the
complete row groups (1,000 rows each) of a Parquet capture that was killed.

Common recording options (`fuji-stream` and `fuji-capture`):

| Option | Default | |
|---|---|---|
| `--rate HZ` | 1 | polls per second, at most 20 |
| `--duration SECONDS` | until Ctrl-C | |
| `--name NAME` | the model, e.g. `zpa` | the `device` value of every row |
| `--overflow` | `block` | `block`, `drop_newest` or `drop_oldest` (see [Recording](recording.md)) |
| `--buffer-size N` | 64 | batches waiting before the overflow policy applies |
| `--reconnect` | off | ride out a connection failure; needs a serial port (with `--fixture` it is a usage error) |

## fuji-diag timing

```
fuji-diag timing COM8
fuji-diag timing COM8 --trials 250 --gaps-ms 0,1,2,5 --out timing.json
```

Pairs of one-word reads, each a normal read or one the analyzer answers with
exception 02, in all four pairings, with a busy-waited gap between them and no
retries, in a random order (`--seed` repeats one). The table shows failures
per pairing and gap; `--out` writes every trial as JSON
(`fujilib-diag-timing/1`). It is the method behind the timing defaults
(design §2.4, protocol findings §6.3). The default run is 800 trials, about
40 s. A trial whose first read did not get its answer is not judged
(`first-bad`). What was measured is written also when the run fails or is
stopped with Ctrl-C, and `--out` is replaced only with `--force`.

## fuji-calibrate

A manual zero or span, driven from the host with the analyzer's calibration
keys while the operator switches the gas valves (design §6.5). **It changes the
analyzer's calibration.**

```bash
fuji-calibrate COM8 --channel CH3 --kind zero --plan
fuji-calibrate COM8 --gas CH3=o2 --channel CH3 --kind zero --gas-value 0 --gas-label "N2" --confirm --i-understand-this-is-destructive
fuji-calibrate COM8 --gas CH3=o2 --channel CH3 --kind span --gas-value 20.95 --gas-unit vol% --confirm --i-understand-this-is-destructive
```

1. It prints what the zero or span calibrates: a zero of a channel set to "at
   once" zeroes every channel so set. `--plan` stops here.
2. It checks the panel, key lock, output hold, the instrument errors, and that
   `--gas-value` (in `--gas-unit`) is the channel's calibration-gas setting.
   Anything wrong stops it before the first key.
3. It presses ZERO or SPAN, moves the cursor to the channel and selects it,
   and asks you to switch the inlet to the gas.
4. It prints the reading as it settles, and when it is steady asks
   `Calibrate CH3 now? [y/N]` (`--auto` does not ask), reading the panel on
   while it waits for the answer. If the reading moves again before the key,
   it waits again.
5. It sends the key that calibrates, follows the calibration to its end, and
   prints the readings before and after, the deviation from the gas and the
   detector counts.
6. It writes the run to `--out`, by default
   `fuji-calibration_<serial>_<channel>_<kind>_<time>.json`, a
   `fujilib-calibration/1` document, and ends with `status:`.

Answering `n`, Ctrl-C or any error cancels: the panel is returned to
measurement. Switch the inlet back to the sample afterwards. The `status:` line
says how it ended: `completed`, `failed`, `ambiguous`, `cancelled`, `refused`
(nothing sent), `stopped` (an error after the first key), `not_clean` (the
panel was not left clean; the message says what to do) or `plan`.

| Option | Meaning |
|---|---|
| `--gas-value`, `--gas-unit`, `--gas-label` | the gas at the inlet; the label is kept in the record |
| `--confirm`, `--i-understand-this-is-destructive` | needed for the keys and for the calibration |
| `--auto` | calibrate once steady, without asking |
| `--window`, `--response-factor`, `--band`, `--tolerance`, `--settle-timeout`, `--max-gap` | the steadiness rule: 30 s or twice the response time, 0.5 %FS, 10 %FS, 600 s, reads at most 5 s apart, by default |
| `--interval` | seconds between reads while the gas settles (0.5) |
| `--out`, `--force`, `--operator`, `--notes` | the record |
| `--no-adc` | do not read the detectors' raw counts |

With `--fixture bench` it runs against the simulated analyzer, where the gas
named flows into the inlet as soon as the wait step opens.
