---
description: fujilib.streaming — samples, poll sources and the recorder.
---

# `fujilib.streaming`

One `Sample` per poll carries a whole `Frame`. A poll source (`PollSourceAdapter`
for one analyzer) is what `record()` polls; the recorder yields a `Recording`
of per-tick batches with a live `AcquisitionSummary` (design §7.6). The guide
is [Recording](../recording.md).

::: fujilib.streaming.sample

::: fujilib.streaming.poll_source

::: fujilib.streaming.recorder

::: fujilib.units
