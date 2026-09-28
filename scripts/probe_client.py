"""Read-only bench probe of fujilib's own Modbus client (design §4.2-§4.5, §10).

Where ``probe_link.py`` and ``probe_map.py`` drive ``anymodbus`` directly, this probe
runs the library's transport, port, client and read procedures against a real analyzer.
Three modes:

``--mode smoke``: every read procedure once (identify with the capability probes, a
poll with and without detail, status, ranges, metadata, settings, named registers, both
logs, clock, A/D), with each transaction's round trip.

``--mode polls``: ``--count`` full polls back to back. Per poll: both blocks' round
trips, the time from the first reply to the second request, the poll's duration and its
attempts; at the end the client's counters. This checks that request timestamps are
taken after the inter-frame gap and measures the sustained poll rate.

``--mode resync``: the late-reply hazard of design §4.2, on hardware. Each trial reads B
once as a reference, idles past the inter-frame gap, then reads A of the same length
and cancels it after ``--cancel-ms`` (A has been sent; its reply is still on the wire),
then reads B again and compares. It runs with the quiet window at 0 and at its default.
A is the readings block, B the error and hold flags, so a B that returns A's words is
recognisable. Only trials whose A reached the wire before the cancellation count.

Every trial is written to the output file, with the arguments, package versions and
script hashes (design §11, "Evidence").

**Read-only by construction.** The probe hands the read procedures a client whose write
methods raise before any I/O; nothing in it can change analyzer state.

Usage::

    uv run python scripts/probe_client.py --port COM8 --address 1 --mode smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from fujilib.config import DEFAULTS
from fujilib.devices import reads
from fujilib.devices.capability import Availability, Capability
from fujilib.devices.decode import label_channels
from fujilib.errors import FujiError
from fujilib.protocol.modbus.port import ModbusPort
from fujilib.protocol.modbus.read_plan import BlockRead
from fujilib.transport.base import SerialSettings
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fujilib._deadline import Deadline
    from fujilib.devices.models import TransferTiming
    from fujilib.protocol.modbus.client import BlockReply, ClientCounters, ModbusClient, PlanReply

FC04 = 0x04
#: Resync mode: the readings block (live values) and the flags block (static at rest).
BLOCK_A = BlockRead(FC04, 0x0000, 36)
BLOCK_B = BlockRead(FC04, 0x0083, 36)
#: Resync mode: idle between trials, so each starts on a quiet line.
TRIAL_SETTLE_S = 0.3
#: Resync mode: idle after the reference read, longer than the inter-frame gap as the
#: Windows timer stretches it (about 16 ms), so A goes out as soon as it is asked for
#: and the cancellation lands while A's reply is on the wire, not while the client is
#: still waiting out the gap.
PRE_A_IDLE_S = 0.04


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class ReadOnlyClient:
    """A client whose write methods refuse before any I/O."""

    def __init__(self, client: ModbusClient) -> None:
        self._client = client

    @property
    def address(self) -> int:
        return self._client.address

    @property
    def label(self) -> str:
        return self._client.label

    @property
    def counters(self) -> ClientCounters:
        return self._client.counters

    @property
    def recoverable_error_count(self) -> int:
        return self._client.recoverable_error_count

    async def read(
        self, block: BlockRead, *, deadline: Deadline | None = None, command: str = "read"
    ) -> BlockReply:
        return await self._client.read(block, deadline=deadline, command=command)

    async def read_plan(
        self, plan: Sequence[BlockRead], *, deadline: Deadline | None = None, command: str = "read"
    ) -> PlanReply:
        return await self._client.read_plan(plan, deadline=deadline, command=command)

    async def write_register(self, *args: object, **kwargs: object) -> TransferTiming:
        msg = "this probe is read-only"
        raise RuntimeError(msg)

    async def write_registers(self, *args: object, **kwargs: object) -> TransferTiming:
        msg = "this probe is read-only"
        raise RuntimeError(msg)


def ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


def gap_ms(before: TransferTiming, after: TransferTiming) -> float:
    return round((after.t_request_mono_ns - before.t_reply_mono_ns) / 1e6, 3)


def spread(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "median": round(statistics.median(ordered), 3),
        "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "max": ordered[-1],
    }


def counters(client: ReadOnlyClient) -> dict[str, Any]:
    c = client.counters
    return {
        "requests": c.requests,
        "retries": c.retries,
        "recovered": c.recovered,
        "failures": {k.value: v for k, v in c.failures.items()},
    }


# --- Modes -----------------------------------------------------------------------------


async def smoke(client: ReadOnlyClient) -> dict[str, Any]:
    result: dict[str, Any] = {}
    identity = await reads.read_identity(client)
    result["identity"] = {
        "type_code": identity.type_code.raw,
        "serial_number": identity.serial_number,
        "nonzero": sorted(c.value for c in identity.nonzero),
        "availability": {c.name: a.value for c, a in identity.availability.items()},
        "latency_ms": [ms(t.latency_s) for t in identity.timings],
    }
    out(f"identity  {identity.type_code.raw} {identity.serial_number} {result['identity']}")
    channels = label_channels(identity.nonzero, type_code=identity.type_code)
    frame = await reads.read_frame(client, channels)
    assert frame.status_timing is not None
    result["poll"] = {
        "readings": [
            {"channel": r.channel.value, "value": r.value, "unit": r.unit.value, "state": r.state}
            for r in frame.readings
        ],
        "latency_ms": [ms(frame.readings_timing.latency_s), ms(frame.status_timing.latency_s)],
        "gap_ms": gap_ms(frame.readings_timing, frame.status_timing),
    }
    out(f"poll      {result['poll']}")
    quick = await reads.read_frame(client, channels, detail=False)
    result["poll_without_detail"] = {"states": sorted({r.state.value for r in quick.readings})}
    status = await reads.read_status(client)
    result["status"] = {
        "instrument_error": status.analyzer.instrument_error,
        "calibration_error": status.analyzer.calibration_error,
        "errors": sorted(int(e) for e in status.analyzer.errors),
        "hold": [c.value for c, s in status.channels.items() if s.hold],
    }
    out(f"status    {result['status']}")
    ranges = await reads.read_ranges(client)
    result["ranges_match_identify"] = ranges == identity.ranges
    clock = identity.availability.get(Capability.CLOCK) is Availability.SUPPORTED
    meta = await reads.read_metadata(
        client,
        serial_number=identity.serial_number,
        ranges=ranges,
        channels=channels,
        clock=clock,
    )
    result["metadata"] = {
        "response_time_o2_s": meta.response_time_o2_s,
        "response_time_ndir_s": list(meta.response_time_ndir_s),
        "hold_mode": str(meta.hold_mode),
        "output_hold": meta.output_hold,
        "clock": meta.clock.isoformat() if meta.clock else None,
        "clock_read_at": meta.clock_read_at.isoformat() if meta.clock_read_at else None,
    }
    out(f"metadata  {result['metadata']}")
    settings = await reads.read_settings(client, ranges=ranges)
    result["settings_decoded"] = len(settings)
    names = ["response_time.o2", "output_hold.enabled", "reading.ch3.value"]
    registers = await reads.read_registers(client, names, ranges=ranges)
    result["registers"] = {n: str(v.value) for n, v in registers.items()}
    log = await reads.read_error_log(client)
    result["error_log"] = [f"{int(e.code)} {e.channel} day {e.at.day}" for e in log]
    out(f"error log {len(log)} entries, newest {result['error_log'][:1]}")
    try:
        entries = await reads.read_calibration_log(client, "CH1")
        result["calibration_log_ch1"] = len(entries)
    except FujiError as exc:
        result["calibration_log_ch1"] = type(exc).__name__
    out(f"cal log   {result['calibration_log_ch1']}")
    if clock:
        reading = await reads.read_clock(client)
        result["clock"] = {
            "clock": reading.clock.isoformat(),
            "host_utc": reading.read_at.isoformat(),
        }
        out(f"clock     {result['clock']}")
    if identity.availability.get(Capability.ADC_VALUES) is Availability.SUPPORTED:
        adc = await reads.read_adc(client)
        result["adc"] = {"reference_voltage": adc.reference_voltage, "inputs": list(adc.inputs)}
        out(f"adc       {result['adc']}")
    result["counters"] = counters(client)
    out(f"counters  {result['counters']}")
    return result


async def polls(client: ReadOnlyClient, count: int) -> dict[str, Any]:
    identity = await reads.read_identity(client, probe=False)
    channels = label_channels(identity.nonzero, type_code=identity.type_code)
    trials: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index in range(count):
        before = client.counters.requests
        t0 = time.perf_counter()
        try:
            frame = await reads.read_frame(client, channels)
        except FujiError as exc:
            trials.append({"index": index, "error": f"{type(exc).__name__}: {exc}"})
            continue
        assert frame.status_timing is not None
        trials.append(
            {
                "index": index,
                "block1_ms": ms(frame.readings_timing.latency_s),
                "block2_ms": ms(frame.status_timing.latency_s),
                "between_ms": gap_ms(frame.readings_timing, frame.status_timing),
                "poll_ms": ms(time.perf_counter() - t0),
                "attempts": client.counters.requests - before,
            }
        )
    elapsed = time.perf_counter() - started
    good = [t for t in trials if "error" not in t]
    summary = {
        "polls": count,
        "failed": count - len(good),
        "polls_per_s": round(count / elapsed, 2),
        "block1_ms": spread([t["block1_ms"] for t in good]),
        "block2_ms": spread([t["block2_ms"] for t in good]),
        "between_ms": spread([t["between_ms"] for t in good]),
        "poll_ms": spread([t["poll_ms"] for t in good]),
        "counters": counters(client),
    }
    for key, value in summary.items():
        out(f"{key:12s} {value}")
    return {"summary": summary, "trials": trials}


async def resync(
    port: ModbusPort, client: ReadOnlyClient, trials: int, cancel_s: float
) -> list[Any]:
    rows: list[dict[str, Any]] = []
    for trial in range(trials):
        await anyio.sleep(TRIAL_SETTLE_S)
        reference = await client.read(BLOCK_B, command="reference")
        await anyio.sleep(PRE_A_IDLE_S)
        sent_before = client.counters.requests
        start = time.perf_counter()
        with anyio.move_on_after(cancel_s) as scope:
            await client.read(BLOCK_A, command="cancelled")
        cancelled_after = time.perf_counter() - start
        row: dict[str, Any] = {
            "trial": trial,
            "resync_window_s": port.resync_window,
            "cancelled": scope.cancelled_caught,
            "a_sent": client.counters.requests > sent_before,
            "cancelled_after_ms": ms(cancelled_after),
        }
        before = client.counters.requests
        start = time.perf_counter()
        try:
            again = await client.read(BLOCK_B, command="after")
        except FujiError as exc:
            row["outcome"] = f"error:{type(exc).__name__}"
        else:
            row["outcome"] = "ok" if again.words == reference.words else "wrong_words"
            row["latency_ms"] = ms(again.timing.latency_s)
            if again.words != reference.words:
                row["words"] = list(again.words)
                row["reference"] = list(reference.words)
        row["after_ms"] = ms(time.perf_counter() - start)
        row["attempts"] = client.counters.requests - before
        rows.append(row)
    return rows


# --- Entry point -------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    transport = await SerialTransport.open(SerialSettings(port=args.port))
    payload: dict[str, Any] = {}
    try:
        if args.mode == "resync":
            rows: list[Any] = []
            for window in (0.0, DEFAULTS.resync_window_s):
                async with ModbusPort(transport, resync_window=window) as port:
                    client = ReadOnlyClient(port.client(args.address))
                    batch = await resync(port, client, args.trials, args.cancel_ms / 1000.0)
                    outcomes: dict[str, int] = {}
                    for row in batch:
                        if row["cancelled"] and row["a_sent"]:
                            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
                    cancelled = sum(r["cancelled"] and r["a_sent"] for r in batch)
                    out(f"window {window:.3f} s: {outcomes} ({cancelled} cancelled after sending)")
                    out(f"  counters {counters(client)}")
                    rows += batch
            payload = {"trials": rows}
        else:
            async with ModbusPort(transport) as port:
                client = ReadOnlyClient(port.client(args.address))
                payload = (
                    await smoke(client) if args.mode == "smoke" else await polls(client, args.count)
                )
    finally:
        await transport.aclose()
    ended = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = Path(args.out) / f"probe_client_{args.mode}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "probe": f"probe_client --mode {args.mode}",
        "port": args.port,
        "address": args.address,
        "defaults": {
            "request_timeout_s": DEFAULTS.request_timeout_s,
            "inter_frame_idle_s": DEFAULTS.inter_frame_idle_s,
            "startup_settle_s": DEFAULTS.startup_settle_s,
            "read_retries": DEFAULTS.read_retries,
            "resync_window_s": DEFAULTS.resync_window_s,
        },
        "count": args.count if args.mode == "polls" else None,
        "trials": args.trials if args.mode == "resync" else None,
        "cancel_ms": args.cancel_ms if args.mode == "resync" else None,
        "blocks": {"A": BLOCK_A.key, "B": BLOCK_B.key} if args.mode == "resync" else None,
        "started_utc": started.isoformat(),
        "ended_utc": ended.isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {p: version(p) for p in ("fujilib", "anymodbus", "anyserial", "anyio")},
        "sha256": {"probe_client.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    }
    path.write_text(json.dumps({"meta": meta, **payload}, indent=1, default=str), encoding="utf-8")
    out(f"\nwritten to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1)
    parser.add_argument("--mode", choices=("smoke", "polls", "resync"), default="smoke")
    parser.add_argument("--count", type=int, default=200, help="polls mode: polls to run")
    parser.add_argument("--trials", type=int, default=30, help="resync mode: trials per window")
    parser.add_argument("--cancel-ms", type=float, default=15.0, help="resync mode: cancel A after")
    parser.add_argument("--out", default="probe_out")
    args = parser.parse_args(argv)
    return anyio.run(run, args)


if __name__ == "__main__":
    raise SystemExit(main())
