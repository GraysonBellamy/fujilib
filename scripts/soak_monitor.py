"""Run a long recording and log its memory use, for a 24-hour recording on the bench.

It starts the command after ``--`` (normally ``fuji-capture``), then every
``--every`` seconds writes one JSON line: the elapsed time, and the resident
memory, CPU time and handle count of the command's whole process tree (a
console-script launcher runs Python as its child). When the command ends it
writes its exit code and exits with it. Ctrl-C reaches the command too, which
stops cleanly; this script then waits for it to finish.

Needs ``psutil``, which fujilib does not depend on::

    uv run --with psutil python scripts/soak_monitor.py --log soak_rss.jsonl -- \
        fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 \
        --rate 1 --duration 86400 --out soak.parquet --reconnect

Check the result with ``scripts/check_soak.py`` (design §12). The log is
started afresh on every run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil


def tree_usage(root: psutil.Process) -> dict[str, float | int]:
    """Memory, CPU time and handles of ``root`` and its children."""
    rss = cpu = handles = 0.0
    try:
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        processes = []
    for proc in processes:
        try:
            with proc.oneshot():
                rss += proc.memory_info().rss
                times = proc.cpu_times()
                cpu += times.user + times.system
                if hasattr(proc, "num_handles"):
                    handles += proc.num_handles()
                else:
                    handles += proc.num_fds()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"rss_mb": round(rss / 2**20, 3), "cpu_s": round(cpu, 3), "handles": int(handles)}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ours, command = (
        (argv[: argv.index("--")], argv[argv.index("--") + 1 :]) if "--" in argv else (argv, [])
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, help="JSON-lines file to write")
    parser.add_argument("--every", type=float, default=600.0, help="seconds between samples")
    args = parser.parse_args(ours)
    if not command:
        parser.error("give the command to run after --")
    executable = shutil.which(command[0]) or command[0]
    log = Path(args.log)
    log.write_text("", encoding="utf-8")  # one run per log
    started = time.monotonic()

    def write(record: dict[str, object]) -> None:
        record = {
            "t": datetime.now(UTC).isoformat(),
            "elapsed_s": round(time.monotonic() - started, 1),
            **record,
        }
        with log.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record) + "\n")

    child = subprocess.Popen([executable, *command[1:]])  # noqa: S603 - the operator's own command
    root = psutil.Process(child.pid)
    write({"event": "start", "pid": child.pid, "command": command})
    code: int | None = None
    while code is None:
        try:
            code = child.wait(timeout=args.every)
        except subprocess.TimeoutExpired:
            write({"event": "sample", **tree_usage(root)})
        except KeyboardInterrupt:
            continue  # the command got Ctrl-C too; wait for it to finish
    write({"event": "exit", "code": code})
    return code


if __name__ == "__main__":
    sys.exit(main())
