"""Zero a Fuji ZP analyzer's O2 channel from the host, with the operator at the gas valves.

    python examples/remote_zero.py COM8

**This changes the analyzer's calibration.** fujilib presses ZERO, moves the
cursor to CH3 and selects it; the analyzer then waits for the gas. Switch the
inlet to zero gas (nitrogen) when asked. fujilib waits until the O2 reading
is steady near 0, asks, and sends the key that calibrates. Ctrl-C or "n"
cancels, and the panel is returned to measurement either way.

The ``fujilib-calibration/1`` record is printed at the end. ``fuji-calibrate``
does the same from the command line.
"""

from __future__ import annotations

import json
import sys

import anyio

from fujilib import open_device
from fujilib.devices.keys import CalibrationGas

#: The bench rig's channels; assert your own (design §2.9).
CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}


async def main(port: str) -> None:
    async with await open_device(port, channel_map=CHANNEL_MAP) as anz:
        plan = await anz.plan_manual_calibration("CH3", "zero")
        print("This zero calibrates", ", ".join(c.value for c in plan.channels))
        gas = CalibrationGas(0.0, label="N2")
        run = anz.manual_calibration(plan, gas=gas, confirm=True, adc=True)
        async with run:
            print("Switch the inlet to zero gas now; waiting for the reading to settle")
            verdict = await run.wait_steady()
            print("Steady:", "; ".join(verdict.reasons))
            answer = await anyio.to_thread.run_sync(input, "Zero CH3 now? [y/N] ")
            if answer.strip().lower() in {"y", "yes"}:
                event = await run.calibrate(confirm=True)
                print(event.outcome.value, dict(event.deviations))
        if run.result is not None:
            record = run.result.as_record(info=anz.info, port=anz.port, address=anz.address)
            print(json.dumps(record, indent=2))


if __name__ == "__main__":
    anyio.run(main, sys.argv[1])
