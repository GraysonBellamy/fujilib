"""Read-only connectivity probe for a Fuji ZP-series gas analyzer.

Opens one serial port at the analyzer's fixed framing (38400 8-N-1), asks each
candidate station for the first three characters of its type code (input
registers 31097-31099, FC04) and reports who answered.

Read-only by construction: see :mod:`_probe_common`.

Usage::

    uv run --no-project --with anymodbus --with anyserial \
        python scripts/probe_connect.py --port COM8 --addresses 1-31
"""

from __future__ import annotations

import argparse
import logging
import sys

import anyio
from _probe_common import (
    BAUDRATE,
    FC_READ_INPUT,
    PARITY,
    ReadOnlyStation,
    attempt,
    open_bus,
    parse_addresses,
    serial_config,
    windows_port,
    words_to_text,
)
from anyserial import open_serial_port

#: Relative address of type-code digit 1 (register 31097).
TYPE_CODE_ADDRESS = 0x0448


async def listen(args: argparse.Namespace) -> int:
    """Receive only: report any bytes seen on the line. Sends nothing."""
    path = windows_port(args.port)
    sys.stdout.write(f"listening on {path} for {args.listen:.1f} s (nothing is sent)\n")
    total = 0
    async with await open_serial_port(path, serial_config(args.baud, args.parity)) as port:
        with anyio.move_on_after(args.listen):
            while True:
                chunk = await port.receive(4096)
                total += len(chunk)
                sys.stdout.write(f"  rx {len(chunk):4d}  {chunk[:32].hex(' ')}\n")
    sys.stdout.write(f"{total} byte(s) received\n")
    return 0


async def run(args: argparse.Namespace) -> int:
    if args.listen > 0:
        return await listen(args)
    addresses = parse_addresses(args.addresses)
    sys.stdout.write(f"opening {args.port} at {args.baud} 8-{args.parity[0].upper()}-1\n")
    answered = 0
    async with open_bus(
        args.port, baud=args.baud, parity=args.parity, timeout=args.timeout, idle=args.idle
    ) as bus:
        for address in addresses:
            result = await attempt(
                ReadOnlyStation(bus.slave(address)), FC_READ_INPUT, TYPE_CODE_ADDRESS, 3
            )
            if result.outcome == "silent" and not args.verbose:
                continue
            raw = " ".join(f"{w:04X}" for w in result.words)
            text = words_to_text(result.words) if result.words else ""
            sys.stdout.write(f"station {address:2d}: {result.describe()}  {raw}  {text!r}\n")
            if result.outcome != "silent":
                answered += 1
    if not answered:
        sys.stdout.write(f"no station answered on {args.port} ({len(addresses)} probed)\n")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True, help="serial port, e.g. COM8")
    parser.add_argument("--addresses", default="1", help='stations: "1", "1,2" or "1-31"')
    parser.add_argument("--timeout", type=float, default=0.3, help="per-request timeout, s")
    parser.add_argument("--idle", type=float, default=0.005, help="inter-frame idle gap, s")
    # The analyzer's framing is fixed; these two only rule a mismatch out.
    parser.add_argument("--baud", type=int, default=BAUDRATE, help="baud rate (diagnostic)")
    parser.add_argument("--parity", choices=sorted(PARITY), default="none")
    parser.add_argument("--listen", type=float, default=0.0, help="receive only for N s")
    parser.add_argument("--verbose", action="store_true", help="also list silent stations")
    parser.add_argument("--frames", action="store_true", help="log tx/rx frames as hex")
    args = parser.parse_args(argv)
    if args.frames:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
    return anyio.run(run, args)


if __name__ == "__main__":
    raise SystemExit(main())
