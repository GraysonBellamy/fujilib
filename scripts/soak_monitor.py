"""Run a long recording and log its memory use, for the 12-hour recording on the bench.

It starts the command after ``--`` (normally ``fuji-capture``), then every
``--every`` seconds writes one JSON line: the elapsed time, and the command's
whole process tree (a console-script launcher runs Python as its child):
resident memory, private memory, CPU time and handle count. Private memory is
what a leak grows; on Windows the resident figure is the working set, which
the system trims and refills. When the command ends it writes its exit code
and exits with it.

Ctrl-C and Ctrl-Break reach the command, which stops cleanly; this script
waits for it to finish. On Windows a window opened by a process that ignores
Ctrl-C (``start`` from a non-interactive shell, for one) passes that on to
everything started in it, so this script turns Ctrl-C back on before it
starts the command.

Needs ``psutil``, which fujilib does not depend on::

    uv run --with psutil python scripts/soak_monitor.py --log soak_rss.jsonl -- \
        fuji-capture COM8 --gas CH1=co2 --gas CH2=co --gas CH3=o2 \
        --rate 1 --duration 43200 --out soak.parquet --reconnect

Check the result with ``scripts/check_soak.py`` (design §12). The log is
started afresh on every run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil


def take_console_events() -> None:
    """Let Ctrl-C and Ctrl-Break reach the command, and outlive them here."""
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415 - Windows only

        # Clears the ignore-Ctrl-C flag, which the command inherits when started.
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            # A handler, not SIG_IGN: a POSIX child would inherit an ignored signal.
            signal.signal(getattr(signal, name), lambda signum, frame: None)


def private_bytes(proc: psutil.Process, info: object) -> int:
    """Memory the process alone holds: private bytes on Windows, else the unique set size."""
    private = getattr(info, "private", None)
    return int(private) if private is not None else int(proc.memory_full_info().uss)


def tree_usage(root: psutil.Process) -> dict[str, float | int]:
    """Memory, CPU time and handles of ``root`` and its children."""
    rss = private = cpu = handles = 0.0
    try:
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        processes = []
    for proc in processes:
        try:
            with proc.oneshot():
                info = proc.memory_info()
                rss += info.rss
                private += private_bytes(proc, info)
                times = proc.cpu_times()
                cpu += times.user + times.system
                if hasattr(proc, "num_handles"):
                    handles += proc.num_handles()
                else:
                    handles += proc.num_fds()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {
        "rss_mb": round(rss / 2**20, 3),
        "private_mb": round(private / 2**20, 3),
        "cpu_s": round(cpu, 3),
        "handles": int(handles),
    }


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

    take_console_events()
    child = subprocess.Popen([executable, *command[1:]])  # noqa: S603 - the operator's own command
    root = psutil.Process(child.pid)
    write({"event": "start", "pid": child.pid, "command": command})
    code: int | None = None
    while code is None:
        try:
            code = child.wait(timeout=args.every)
        except subprocess.TimeoutExpired:
            write({"event": "sample", **tree_usage(root)})
    write({"event": "exit", "code": code})
    return code


if __name__ == "__main__":
    sys.exit(main())
