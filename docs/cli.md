---
description: The fuji-* command-line tools — read, discover, decode, dump settings, stream, capture and diagnose a Fuji ZP-series analyzer.
---

# Commands

Every command is read-only: it sends read requests and nothing else. Each is
also a function, `main(argv) -> int`, in `fujilib.cli`.

| Command | What it does |
|---|---|
| `fuji-read` | read an analyzer once: identity, a poll, status, metadata, ranges, logs, clock, A/D, snapshot |
| `fuji-discover` | find ZP analyzers on named ports (or every port, with `--all-ports`) |
| `fuji-decode` | decode MODBUS frames or a register dump, offline |
| `fuji-configure dump` | read every setting and write it as a `fujilib-settings/1` JSON document |
| `fuji-stream` | poll at a fixed rate and print each poll |
| `fuji-capture` | record to a CSV or Parquet file, with the analyzer's metadata beside it |
| `fuji-diag timing` | measure the gap the analyzer needs between requests |

**Exit codes.** 0 on success, 1 for a library error (printed as `error: ...`),
2 for bad arguments. `fuji-discover` also exits 2 when it finds nothing.
`fuji-stream` and `fuji-capture` exit 0 when stopped with Ctrl-C, after
writing and closing what they recorded.

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
  A Parquet file also carries the starting version in its metadata, as
  `fujilib.capture`.

Existing files are not replaced without `--force`; a missing directory for
`--out` is created. A connection failure ends
the capture with exit code 1 unless `--reconnect` asks for the port to be
reopened; either way the files are complete up to that point. For an
unattended recording, CSV is the safer format: a Parquet file is readable only
once it is closed.

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
