"""Print every manual zero or span made at a Fuji ZP analyzer's front panel. Read-only.

    python examples/watch_manual_calibration.py COM8

Each calibration is printed as one JSON line when it ends, a
``fujilib-calibration/1`` document: what it calibrated, how it ended and why,
the readings before and after, the calibration gas, the deviation from it and
the raw A/D counts when it ran. Stop with Ctrl-C.
"""

from __future__ import annotations

import contextlib
import json
import sys

import anyio

from fujilib import open_device
from fujilib.devices.panel import calibration_record
from fujilib.errors import FujiTimeoutError

#: The bench rig's channels; assert your own (design §2.9).
CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}


async def main(port: str) -> None:
    async with await open_device(port, channel_map=CHANNEL_MAP) as anz:
        print(f"watching {port}; make a zero or span at the panel", file=sys.stderr)
        while True:
            try:
                event = await anz.wait_for_manual_calibration(timeout=3600, adc=True)
            except FujiTimeoutError:
                continue
            record = calibration_record(event, info=anz.info, port=anz.port, address=anz.address)
            print(json.dumps(record), flush=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(main, sys.argv[1])
