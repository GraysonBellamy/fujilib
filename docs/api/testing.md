---
description: fujilib.testing — arrow-format frame fixtures, builders for readings and frames, the simulated analyzer and the bench bank.
---

# `fujilib.testing`

Everything needed to develop and test without hardware (design §10): the
family's arrow-format frame fixtures, builders for synthetic readings and
frames, a simulated ZP analyzer on a real serial port pair, and the sanitized
bench register bank.

The builders are for code that consumes fujilib's models rather than the
line: an application's adapter, a sink, a simulator of its own. A built frame
becomes the sample and the row a recording produces:

```python
from fujilib import ChannelId, Gas, ReadingState, Sample, sample_to_row
from fujilib.testing import frame, reading, status

held_o2 = reading(
    ChannelId.CH3, Gas.O2, 2095, 2, channel_status=status(hold=True), state=ReadingState.HOLD
)
sample = Sample.from_frame(frame((held_o2,)), device="zpa", address=1)
row = sample_to_row(sample)  # row["ch3_value"] == 20.95, row["ch3_state"] == "hold"
```

::: fujilib.testing.arrow

::: fujilib.testing.frames

::: fujilib.testing.mock

::: fujilib.testing.pair
