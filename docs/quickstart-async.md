---
description: Open a Fuji ZP-series analyzer, identify it, poll every channel and read its settings, with async Python.
---

# Quickstart: async

fujilib is async-first and runs on [AnyIO](https://anyio.readthedocs.io/), so it
works under asyncio and trio alike. Everything here is read-only: nothing is
written to the analyzer.

```python
import anyio

from fujilib import open_device


async def main() -> None:
    async with await open_device(
        "COM8",  # or "/dev/ttyUSB0"; the station is 1 unless set otherwise on the panel
        channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"},
    ) as anz:
        print(anz.info)  # model, serial number, channels, ranges, capabilities

        frame = await anz.poll()  # every channel and the analyzer's status
        o2 = frame.channel("CH3")
        print(o2.value, o2.unit, o2.state, o2.label_source)

        meta = await anz.read_metadata()  # response times, calibration gases, clock...
        print(meta.response_time_o2_s, meta.calibration_gas)


anyio.run(main)
```

## What `open_device` does

`open_device` opens the serial port (38400 8-N-1, the analyzer's only setting),
identifies the analyzer and returns an `Analyzer`. Identification reads the
type code, serial number, ranges and readings, and probes the features that
depend on firmware. It takes six Modbus transactions, about 0.3 s. The
`async with` block closes the port again, including when something fails.

- `address=` is the station number, 1-31, as set on the front panel.
- `timeout=` is how long to wait for each reply (0.5 s by default).
- A `Transport` can be passed instead of a port name; it stays yours to close.

## Gas labels

Which gas each channel carries comes from `channel_map`. The analyzer's type
code only *suggests* labels, and on the development unit it is out of date. A
channel whose label feeds a calculation must be asserted. Every reading says
where its label came from (`label_source`), and a label you did not assert
has `gas == Gas.UNKNOWN`, with the suggestion in `suggested_gas`.

A channel is *established* when it is asserted, or when its reading has been
non-zero at least once since the analyzer was opened. Only established
channels appear in a frame.

## Validity

Every reading carries a `state`: `ok`, or the most important reason it is not
live: an analyzer or channel error, a calibration, an auto calibration, or
output hold, during which the value is frozen. `reading.valid` is `True` only
for `ok`. It is `None`, not `True`, when the status was not read
(`poll(detail=False)`). Raw values are always kept.

!!! note "Oxygen"
    Over Modbus, O2 arrives with the display's resolution: 0.01 vol% per step
    on the development unit. Modbus O2 is **not validated for
    oxygen-consumption calorimetry** (design §2.11).

## Deadlines

Every method that talks to the analyzer takes a keyword-only `timeout=`, a
deadline for the whole call: waiting for the port, every transaction and any
retry. When it expires the call raises `FujiTimeoutError`.

```python
frame = await anz.poll(timeout=1.0)
```

## Errors

Every exception is a `FujiError` with an `ErrorContext` (port, station,
operation, register). A read that fails in transit is retried twice before it
raises. A connection failure closes the session for good; open the analyzer
again to continue.

## More reads

| Method | Reads |
|---|---|
| `status()`, `channel_status(ch)` | the analyzer's and one channel's status |
| `read_ranges()` | each channel's ranges |
| `read_settings()`, `read_parameter(name)` | holding registers by their names in the [register map](registers.md) |
| `read_error_log()` | the last 14 errors (day, hour and minute only) |
| `read_calibration_log(ch)` | calibration records; firmware 2.24 or later |
| `read_clock()`, `read_adc()` | the analyzer's clock and raw A/D counts (undocumented registers) |
| `snapshot()` | identity and health, from what is cached, with no I/O |

## Changing a setting

A reviewed subset of settings can be written: response times, output hold,
hold, a channel's range, and the calibration gases and scope. Every write
needs `confirm=True`, is refused while the analyzer is calibrating or its
front panel is in a menu, and is read back. See [Safety](safety.md).

```python
result = await anz.write_parameter("response_time.o2", 20, confirm=True)
print(result.state, result.previous.value, result.observed.value)  # verified 15 20

await anz.set_range("CH3", 2, confirm=True)
await anz.set_calibration_gas("CH3", 1, "span", 20.9, unit="vol%", confirm=True)
```

## Finding analyzers

```python
from fujilib import find_devices

results = await find_devices(ports=["COM8"], addresses=range(1, 32))
for result in results:
    if result.device_info is not None:
        print(result.port, result.address, result.model, result.device_info.serial_number)
```

Discovery only reads, but every port it scans receives its probe frames, so
name the ports rather than scanning them all.

## Without hardware

`fujilib.testing` has a simulated analyzer that answers from the development
unit's registers:

```python
from fujilib import open_device
from fujilib.testing import DEFAULT_ZPA_BANK, MockAnalyzer, mock_transport

async with mock_transport(MockAnalyzer(DEFAULT_ZPA_BANK)) as (transport, _line):
    async with await open_device(transport) as anz:
        print(await anz.poll())
```

## From the command line

```bash
fuji-read COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2   # identity and one poll
fuji-read COM8 --all --format json                       # everything, as JSON
fuji-discover COM8 --addresses 1-31                      # find stations on a port
fuji-configure dump COM8 --out settings.json             # every setting, by name
fuji-configure diff COM8 --file settings.json            # what applying it would change
fuji-read --fixture bench --all                          # the simulated analyzer
```

## Recording

`record()` polls at a fixed rate; `pipe()` writes the polls to a sink. See
[Recording](recording.md).

```python
from fujilib import CsvSink, PollSourceAdapter, pipe, record

async with (
    CsvSink("run.csv") as sink,
    record(PollSourceAdapter("zpa", anz), rate_hz=1.0, duration=600) as rec,
):
    await pipe(rec, sink)
```
