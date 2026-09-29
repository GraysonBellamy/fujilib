---
description: Record a Fuji ZP-series analyzer at a fixed rate into memory, CSV or Parquet, with fujilib's recorder and sinks.
---

# Recording

`record()` polls one or more analyzers at a fixed rate and hands you one
*batch* per tick: a mapping from each analyzer's name to its `Sample`. A
sink writes the batches to memory, CSV or Parquet. Everything here is
read-only. The design is in [design §7.6](design.md).

```python
import anyio

from fujilib import ParquetSink, PollSourceAdapter, open_device, pipe, record


async def main() -> None:
    async with await open_device(
        "COM8", channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}
    ) as anz:
        source = PollSourceAdapter("zpa", anz)
        channels = [c.channel for c in anz.channels]
        async with (
            ParquetSink("run.parquet", channels=channels) as sink,
            record(source, rate_hz=1.0, duration=3600) as rec,
        ):
            await pipe(rec, sink)
        print(rec.summary)


anyio.run(main)
```

Or iterate the batches yourself:

```python
async with record(source, rate_hz=1.0) as rec:
    async for batch in rec:
        sample = batch["zpa"]
        if sample.error is None:
            print(sample.frame.channel("CH3").value)
```

## Samples and rows

One `Sample` per analyzer per tick carries the whole `Frame`: every
established channel and the analyzer's status, read in two Modbus
transactions. `sample_to_row(sample)` flattens it into one wide row of
scalars: a header (`device`, `address`, `t_mono_ns`, `t_utc`, `latency_s`,
…), then per channel `chN_value`, `chN_raw`, `chN_decimals`, `chN_unit`,
`chN_gas`, `chN_label_source`, `chN_state`, `chN_valid`, `chN_hold`,
`chN_calibrating` and `chN_errors`, then the analyzer's errors and alarms, then
`error_type` and `error_message`.

- **Timestamps.** `t_mono_ns` and `t_utc` are the midpoint of the request and
  the reply of the block that holds every concentration. They are host times;
  the analyzer's own clock is never used for them. The analyzer's response
  time (15 s on the development unit) is applied inside the analyzer and
  is recorded in its metadata, not subtracted from timestamps.
- **The columns are fixed when the recording starts.** Every sample carries
  the channels established then (`Sample.channels`), so every row of a
  recording, a failed poll included, has the same keys and the same types. A
  channel that is first seen alive later is left out of this recording's
  rows; assert every channel you need with `channel_map`.
- **A failed poll is a row too.** Its `frame` is `None`, its `error` is set,
  and its row carries `None` in every reading and analyzer column, with the
  error's type and message. Gaps are recorded, never dropped.
- **Manual calibrations are not in the rows.** A zero or span made at the
  front panel marks its channels `calibrating`. Feed the frames to a
  `ManualCalibrationTracker` (`fujilib.devices.panel`) to get one event per
  calibration: its channels, how it ended, and the readings before and after.
  Frames read at 1 Hz can miss the second or two a calibration runs; the event
  then rests on an undocumented register, or says it is ambiguous.

  ```python
  from fujilib.devices.panel import ManualCalibrationTracker, PanelObservation

  tracker = ManualCalibrationTracker()
  for sample in samples:  # of one analyzer, in order
      if sample.frame is not None and (seen := PanelObservation.from_frame(sample.frame)):
          if event := tracker.feed(seen):
              print(event.kind, event.outcome, event.channels)
  ```

## The schedule

Tick *k* is due `k / rate_hz` seconds after the first, which runs at once.
When a poll overruns so far that whole slots pass, the recorder skips them
and counts them in `summary.samples_late`; it never polls in a burst to catch
up. One analyzer's poll takes about 0.12 s through an FTDI adapter, so about
7-8 Hz is the practical ceiling for one station, and 1 Hz is the usual rate.

`duration` makes every tick due before it has passed, `ceil(duration * rate_hz)`
of them and at least one; without it, the recording runs until the `async with`
block exits.

## When the consumer is slow

The stream holds `buffer_size` batches (64 by default). When it is full,
`overflow` decides:

| `OverflowPolicy` | What happens |
|---|---|
| `BLOCK` (default) | the recorder waits; the consumer sets the pace, and ticks missed meanwhile are counted late |
| `DROP_NEWEST` | the new batch is discarded |
| `DROP_OLDEST` | the oldest waiting batch is discarded to make room |

A batch is dropped whole, never split, and counted in
`summary.samples_dropped`.

## Connection failures

A timeout, a damaged reply or any other failed poll is an error row, and the
recording goes on. A **connection failure** (the adapter unplugged, the port
gone) ends it: the tick's error row is still delivered, the stream ends, and
leaving the `async with` block raises `FujiConnectionError`.

To ride out a connection failure instead, pass a `ReconnectPolicy`:

```python
async with record(source, rate_hz=1.0, reconnect=ReconnectPolicy()) as rec:
    ...
```

Every tick of the outage is then an error row, and the analyzer is reopened on
the policy's schedule (0.5 s, 1, 2, 5, 10, then every 30 s by default).
Reopening opens the port again under the same settings and identifies the
analyzer, which must be the one that was open: the same serial number and type
code. Only an analyzer opened by port name can be reopened, not one opened on
a transport you passed in. You can also reopen one yourself with
`await anz.reopen()`.

## The summary

`rec.summary` is updated live and finished when the recording stops, however
it stops. It counts polls, not rows.

| Field | Meaning |
|---|---|
| `samples_emitted` | batches put on the stream (and not dropped since); once stopped, exactly the batches the consumer received |
| `samples_late` | ticks skipped because a poll overran |
| `samples_dropped` | batches the overflow policy discarded, and batches still unread when the recording stopped |
| `error_samples` | samples of failed polls |
| `disconnects`, `reconnects` | connection failures (each outage ridden out, and the one that ends a recording), and outages ended by reopening |
| `max_drift_ms` | the latest a poll started after its slot |
| `target_total_samples` | the ticks a `duration` asked for |

For a recording that runs to its end,
`samples_emitted + samples_dropped + samples_late == target_total_samples`.
The traffic counters (requests, retries, failures by kind) are on
`anz.session.counters`.

## Sinks

| Sink | Notes |
|---|---|
| `InMemorySink` | keeps every sample; for tests and short recordings |
| `CsvSink` | flushed after every write, so a process that dies loses only what was not written yet: what `pipe()` held (at most `flush_interval` seconds or `batch_size` samples) and what waited in the recording's buffer. Text is quoted and numbers are not, so an empty field is `None` and `""` is empty text; read it back with `csv.QUOTE_NOTNULL` |
| `ParquetSink` | the `parquet` extra (`pip install 'fujilib[parquet]'`); zstd; rows gathered into row groups of 1,000 (`row_group_size`); the file's metadata carries `fujilib.version` and whatever you pass. Readable only once closed, which cancellation and Ctrl-C still do; a killed process leaves a file without its footer, whose complete row groups the repository's `scripts/recover_parquet.py` recovers |

Each fixes its columns from `row_columns(channels)` before the first row:
pass `channels=` to fix them at `open()`, or they are taken from the first
batch. Types never come from the values, so a recording that starts with an
error row still types `ch3_value` as a float. A sample with a channel the
columns lack is refused (`FujiSinkSchemaError`) rather than written without it.
File I/O runs in a worker thread, never on the event loop.

`pipe(rec, sink, batch_size=64, flush_interval=1.0)` writes the batches in
groups, at the latest one `flush_interval` after the first of a group arrived.
When it stops, by the recording ending, an error or cancellation, it takes the
batches already waiting and writes what it holds first. A file written by
`pipe()` has one row per analyzer for every batch the summary counts as
emitted, a recording stopped with Ctrl-C included.

## Blocking code

`fujilib.sync` has the same recorder for code without an event loop:

```python
from fujilib.sync import Fuji, PollSourceAdapter, SyncCsvSink, pipe, record

with (
    Fuji.open("COM8", channel_map={"CH3": "o2"}) as anz,
    record(PollSourceAdapter("zpa", anz), rate_hz=1.0, duration=60) as rec,
    SyncCsvSink("run.csv", portal=anz.portal) as sink,
):
    pipe(rec, sink)
```

`for batch in rec:` iterates the batches instead.

## From the command line

`fuji-stream` prints each poll; `fuji-capture` records to a file with its
metadata beside it. See [Commands](cli.md).
