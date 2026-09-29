"""Print every manual zero or span made at a Fuji ZP analyzer's front panel. Read-only.

    python examples/watch_manual_calibration.py COM8

Each calibration is printed as one JSON line when it ends: what it calibrated,
how it ended and why, the readings before and after, the calibration gas, the
deviation from it and the raw A/D counts when it ran. Stop with Ctrl-C.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import TYPE_CHECKING

import anyio

from fujilib import open_device
from fujilib.errors import FujiTimeoutError

if TYPE_CHECKING:
    from fujilib.devices.panel import ManualCalibrationEvent

#: The bench rig's channels; assert your own (design §2.9).
CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}


def as_json(event: ManualCalibrationEvent, serial_number: str | None) -> dict[str, object]:
    """The event as a JSON object, keyed by channel name."""
    return {
        "serial_number": serial_number,
        "kind": event.kind.value,
        "outcome": event.outcome.value,
        "channels": [c.value for c in event.channels],
        "ranges": {c.value: r for c, r in event.ranges.items()},
        "calibrated_at": event.calibrated_at.isoformat() if event.calibrated_at else None,
        "ended_at": event.ended_at.isoformat(),
        "before": {c.value: r.value for c, r in event.before.items()},
        "after": {c.value: r.value for c, r in event.after.items()},
        "gases": {c.value: g for c, g in event.gases.items()},
        "deviations": {c.value: d for c, d in event.deviations.items()},
        "detector_counts": list(event.adc_before.inputs) if event.adc_before else None,
        "evidence": list(event.evidence),
    }


async def main(port: str) -> None:
    async with await open_device(port, channel_map=CHANNEL_MAP) as anz:
        serial_number = anz.info.serial_number if anz.info else None
        print(f"watching {port}; make a zero or span at the panel", file=sys.stderr)
        while True:
            try:
                event = await anz.wait_for_manual_calibration(timeout=3600, adc=True)
            except FujiTimeoutError:
                continue
            print(json.dumps(as_json(event, serial_number)), flush=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(main, sys.argv[1])
