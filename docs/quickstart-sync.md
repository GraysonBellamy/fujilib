---
description: Use a Fuji ZP-series analyzer from ordinary blocking Python code.
---

# Quickstart: sync

`fujilib.sync` wraps the async core for code that is not async. Each method
has the same name, parameters and defaults as its async twin; it blocks until
the result is in.

```python
from fujilib.sync import Fuji

with Fuji.open("COM8", channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}) as anz:
    frame = anz.poll()
    print(frame.channel("CH3").value)
    print(anz.read_metadata().response_time_o2_s)
```

`Fuji.open` takes the same arguments as [`open_device`](quickstart-async.md) and
closes the analyzer at the end of the `with` block.

## Several analyzers, one event loop

Each `Fuji.open` runs its own event loop in a background thread. To share one,
open a `SyncPortal` and pass it in:

```python
from fujilib.sync import Fuji, SyncPortal

with SyncPortal() as portal:
    with Fuji.open("COM8", portal=portal) as a, Fuji.open("COM9", portal=portal) as b:
        print(a.poll(), b.poll())
```

## Discovery

```python
from fujilib.sync import find_devices

for result in find_devices(ports=["COM8"], addresses=range(1, 32)):
    print(result.port, result.address, result.ok, result.model)
```

## Recording

```python
from fujilib.sync import Fuji, PollSourceAdapter, SyncCsvSink, pipe, record

with (
    Fuji.open("COM8", channel_map={"CH1": "co2", "CH2": "co", "CH3": "o2"}) as anz,
    record(PollSourceAdapter("zpa", anz), rate_hz=1.0, duration=600) as rec,
    SyncCsvSink("run.csv", portal=anz.portal) as sink,
):
    pipe(rec, sink)
```

`for batch in rec:` iterates the polls instead. See [Recording](recording.md).
