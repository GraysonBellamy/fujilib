"""Bench probe of what the analyzer does with writes the manuals leave open (design §13.2).

**This probe writes to the analyzer.** Each step writes one register of a channel
or NDIR component the bench analyzer does not have (``hold.ch5.value``,
``response_time.ndir4``), so no measurement changes, and restores it before it
ends. Every step needs ``--confirm``, and the owner's authorization for the
session. Four steps, each run on its own:

``key-lock`` (§13.2 #12): with key lock switched on at the front panel, does
the analyzer accept, ignore or refuse a setting write over Modbus, and a
return-to-measurement command? The panel's key lock must be on; switch it off
afterwards at the panel.

``persist-write`` then ``persist-check`` (§13.2 #14): write a marker value,
power-cycle the analyzer, and read it back: is a setting written over Modbus
kept without a save step? ``persist-write`` keeps what it wrote in a state
file for ``persist-check``, which restores the register.

``out-of-range``: write 61 and 0 to ``response_time.ndir4`` with the client
directly, past the library's own limit of 1-60 s (the MODBUS manual gives 0-60,
the ZPA manual 1-60): does the analyzer refuse with exception 03, clamp, or
store the value? The client still checks the frozen write envelope.

Every result goes to ``probe_out/probe_write_<step>_<time>.json`` with the
arguments and package versions (design §11, "Evidence").

Usage::

    uv run python scripts/probe_write.py --port COM8 key-lock --confirm
    uv run python scripts/probe_write.py --port COM8 persist-write --confirm
    uv run python scripts/probe_write.py --port COM8 persist-check --confirm
    uv run python scripts/probe_write.py --port COM8 out-of-range --confirm
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from fujilib import FujiError, open_device
from fujilib.errors import FujiModbusError
from fujilib.registry.registers import REGISTRY

if TYPE_CHECKING:
    from fujilib import Analyzer

OUT = Path(__file__).resolve().parent.parent / "probe_out"
STATE = OUT / "probe_write_persist_state.json"
MARKED = "hold.ch5.value"
RANGED = "response_time.ndir4"


def _outcome(exc: BaseException | None) -> dict[str, Any]:
    if exc is None:
        return {"raised": None}
    out: dict[str, Any] = {"raised": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, FujiModbusError) and exc.__cause__ is not None:
        out["exception_code"] = getattr(exc.__cause__, "exception_code", None)
    return out


async def _raw(anz: Analyzer, name: str) -> int:
    return int((await anz.read_parameter(name)).raw)


async def _put_back(anz: Analyzer, name: str, before: int) -> bool:
    """Write ``before`` back to ``name`` through the client, and say whether it holds."""
    await anz.session._client.write_register(
        REGISTRY.resolve(name).address, before, command="probe_restore"
    )
    return await _raw(anz, name) == before


async def key_lock(anz: Analyzer) -> dict[str, Any]:
    if not (await anz.read_parameter("key_lock")).raw:
        msg = "switch key lock on at the front panel first"
        raise SystemExit(msg)
    before = await _raw(anz, MARKED)
    result: dict[str, Any] = {"key_lock": True, "register": MARKED, "before": before}
    try:
        error: BaseException | None = None
        try:
            write = await anz.write_parameter(MARKED, (before + 37) % 101, confirm=True)
            result["write_state"] = write.state.value
        except FujiError as exc:
            error = exc
        result["write"] = _outcome(error)
        result["after"] = await _raw(anz, MARKED)
        error = None
        try:
            command = await anz.return_to_measurement(confirm=True)
            result["command_outcome"] = command.outcome.value
        except FujiError as exc:
            error = exc
        result["command"] = _outcome(error)
    finally:
        result["restored"] = await _put_back(anz, MARKED, before)
    print("switch key lock off at the front panel now")
    return result


async def persist_write(anz: Analyzer) -> dict[str, Any]:
    before = await _raw(anz, MARKED)
    marker = (before + 37) % 101
    write = await anz.write_parameter(MARKED, marker, confirm=True)
    state = {
        "register": MARKED,
        "before": before,
        "marker": marker,
        "write_state": write.state.value,
        "serial_number": anz.info.serial_number if anz.info else None,
        "written_at": datetime.now(UTC).isoformat(),
    }
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"wrote {marker} to {MARKED}; power-cycle the analyzer, then run persist-check")
    return state


async def persist_check(anz: Analyzer) -> dict[str, Any]:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    serial = anz.info.serial_number if anz.info else None
    if state["serial_number"] != serial:
        msg = f"{STATE} was written on analyzer {state['serial_number']!r}, not {serial!r}"
        raise SystemExit(msg)
    now = await _raw(anz, MARKED)
    kept = {state["marker"]: "kept", state["before"]: "lost"}.get(now, "other")
    restored = await _put_back(anz, MARKED, state["before"])
    STATE.unlink()
    return {**state, "after_power_cycle": now, "result": kept, "restored": restored}


async def out_of_range(anz: Analyzer) -> dict[str, Any]:
    address = REGISTRY.resolve(RANGED).address
    before = await _raw(anz, RANGED)
    client = anz.session._client
    trials: list[dict[str, Any]] = []
    try:
        for value in (61, 0):
            error: BaseException | None = None
            try:
                await client.write_register(address, value, command="probe_out_of_range")
            except FujiError as exc:
                error = exc
            try:
                trials.append(
                    {"written": value, **_outcome(error), "read_back": await _raw(anz, RANGED)}
                )
            finally:
                await client.write_register(address, before, command="probe_restore")
    finally:
        restored = await _raw(anz, RANGED) == before
    return {"register": RANGED, "before": before, "trials": trials, "restored": restored}


STEPS = {
    "key-lock": key_lock,
    "persist-write": persist_write,
    "persist-check": persist_check,
    "out-of-range": out_of_range,
}


async def main_async(args: argparse.Namespace) -> Path:
    async with await open_device(args.port, address=args.address) as anz:
        started = datetime.now(UTC)
        result = await STEPS[args.step](anz)
    OUT.mkdir(exist_ok=True)
    path = OUT / f"probe_write_{args.step}_{started:%Y%m%dT%H%M%SZ}.json"
    document = {
        "probe": "probe_write",
        "step": args.step,
        "arguments": {"port": args.port, "address": args.address},
        "started_at": started.isoformat(),
        "versions": {
            "python": platform.python_version(),
            **{p: version(p) for p in ("fujilib", "anymodbus", "anyserial", "anyio")},
        },
        "result": result,
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--address", type=int, default=1)
    parser.add_argument("step", choices=sorted(STEPS))
    parser.add_argument("--confirm", action="store_true", help="Required: this writes.")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("this probe writes to the analyzer; pass --confirm")
    path = anyio.run(main_async, args)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
