"""Record a Fuji ZP analyzer to Parquet at 1 Hz, with its metadata in the file.

Needs the ``parquet`` extra: ``pip install 'fujilib[parquet]'``. Read-only.

    python examples/record_to_parquet.py COM8 run.parquet 600

Stop early with Ctrl-C: the file is closed and readable either way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

from fujilib import ParquetSink, PollSourceAdapter, open_device, pipe, record

#: The bench rig's channels; assert your own (design §2.9).
CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}


async def main(port: str, out: Path, seconds: float) -> None:
    async with await open_device(port, channel_map=CHANNEL_MAP) as anz:
        meta = await anz.read_metadata()
        info = anz.info
        context = {
            "serial_number": info.serial_number if info else None,
            "response_time_o2_s": str(meta.response_time_o2_s),
        }
        channels = [c.channel for c in anz.channels]
        async with (
            ParquetSink(out, channels=channels, metadata=context) as sink,
            record(PollSourceAdapter("zpa", anz), rate_hz=1.0, duration=seconds) as rec,
        ):
            await pipe(rec, sink)
        summary = rec.summary
        print(json.dumps({"polls": summary.samples_emitted, "failed": summary.error_samples}))


if __name__ == "__main__":
    anyio.run(main, sys.argv[1], Path(sys.argv[2]), float(sys.argv[3]))
