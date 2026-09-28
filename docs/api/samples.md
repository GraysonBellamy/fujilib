---
description: Samples, poll sources, wide rows and pint unit strings.
---

# Samples and rows

One `Sample` per poll carries a whole `Frame`; `sample_to_row()` flattens it into
one wide row with a fixed set of scalar columns (design §7.6). A
`PollSourceAdapter` presents an analyzer as a source of polls, each a
`DeviceResult`.

::: fujilib.streaming.sample

::: fujilib.streaming.poll_source

::: fujilib.sinks.base

::: fujilib.sinks._schema

::: fujilib.units
