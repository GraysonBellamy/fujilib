"""Check a long recording against the hardware exit of design §12.

Reads a ``fuji-capture`` file (CSV or Parquet), its ``.meta.json``, and
optionally the memory log of ``scripts/soak_monitor.py``, and checks:

- **readable output**: the file opens, with exactly the columns of its channels;
- **tick and row counts**: rows equal the polls counted, and polls, late and
  dropped ticks add up to the ticks asked for;
- **timing**: ``t_mono_ns`` always increases, and an interval longer than 1.5
  periods happens only where a tick was late or dropped;
- **error accounting**: error rows equal the failed polls counted (at most
  that, when batches were dropped);
- **status and provenance retention**: every successful row carries each
  channel's gas, asserted label, state and validity;
- **clean shutdown**: the recording ``finished`` (or was ``stopped`` by Ctrl-C);
- **bounded memory**: after the first hour, memory grows by less than
  ``--max-growth-mb``.

It prints a report, writes it as JSON beside the data (``<data>.check.json``)
and exits 0 when every check passes::

    uv run python scripts/check_soak.py soak.parquet --rss soak_rss.jsonl
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fujilib.registry.channels import ChannelId
from fujilib.sinks import row_columns


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".parquet", ".pq"}:
        import pyarrow.parquet as pq  # noqa: PLC0415 - only for a Parquet file

        return pq.read_table(path).to_pylist()
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file, quoting=csv.QUOTE_NOTNULL))
    for row in rows:  # the CSV columns that the checks read as numbers or booleans
        for key, value in list(row.items()):
            if value is None:
                continue
            if key in {"t_mono_ns", "address"}:
                row[key] = int(value)
            elif key.endswith("_valid") or key.endswith("_hold"):
                row[key] = value == "true"
    return rows


def memory_growth(path: Path, *, after_s: float) -> dict[str, float] | None:
    samples = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    points = [(s["elapsed_s"], s["rss_mb"]) for s in samples if s.get("event") == "sample"]
    settled = [p for p in points if p[0] >= after_s]
    if len(settled) < 2:
        return None
    xs, ys = [p[0] / 3600 for p in settled], [p[1] for p in settled]
    slope = statistics.linear_regression(xs, ys).slope if len(set(xs)) > 1 else 0.0
    return {
        "samples": len(points),
        "first_mb": points[0][1],
        "settled_first_mb": ys[0],
        "last_mb": ys[-1],
        "max_mb": max(p[1] for p in points),
        "growth_mb": round(ys[-1] - ys[0], 3),
        "slope_mb_per_h": round(slope, 4),
    }


def check(args: argparse.Namespace) -> dict[str, Any]:
    data = Path(args.data)
    meta = json.loads(Path(str(data) + ".meta.json").read_text(encoding="utf-8"))
    summary = meta.get("summary") or {}
    rate = float(meta["arguments"]["rate_hz"])
    channels = meta["channels"]
    asserted = meta["arguments"]["gas"]
    rows = load_rows(data)
    results: dict[str, Any] = {"data": str(data), "rows": len(rows), "checks": {}}
    checks = results["checks"]

    def record(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    expected = [c.name for c in row_columns(ChannelId(c) for c in channels)]
    found = list(rows[0]) if rows else []
    record("readable output", bool(rows) and found == expected, columns=len(found))
    record(
        "clean shutdown",
        meta["state"] in {"finished", "stopped"},
        state=meta["state"],
        error=meta.get("error"),
    )
    polls, late, dropped = summary.get("polls"), summary.get("late"), summary.get("dropped")
    target = summary.get("target_polls")
    counted = (polls or 0) + (late or 0) + (dropped or 0)
    record(
        "tick and row counts",
        len(rows) == polls and (target is None or meta["state"] == "stopped" or counted == target),
        rows=len(rows),
        polls=polls,
        late=late,
        dropped=dropped,
        target=target,
    )
    stamps = [r["t_mono_ns"] for r in rows]
    intervals = [(b - a) / 1e9 for a, b in itertools.pairwise(stamps)]
    period = 1.0 / rate
    long_gaps = [i for i in intervals if i > 1.5 * period]
    skipped = (late or 0) + (dropped or 0)
    record(
        "timing",
        all(i > 0 for i in intervals) and len(long_gaps) <= skipped,
        median_s=statistics.median(intervals) if intervals else None,
        max_s=max(intervals) if intervals else None,
        intervals_over_1_5_periods=len(long_gaps),
    )
    errors = [r for r in rows if r["error_type"] is not None]
    failed_polls = summary.get("failed_polls") or 0
    record(
        "error accounting",
        len(errors) == failed_polls or (bool(dropped) and len(errors) <= failed_polls),
        error_rows=len(errors),
        failed_polls=summary.get("failed_polls"),
        types=dict(Counter(r["error_type"] for r in errors)),
        disconnects=summary.get("disconnects"),
        reconnects=summary.get("reconnects"),
        traffic=summary.get("traffic"),
    )
    good = [r for r in rows if r["error_type"] is None]
    missing: Counter[str] = Counter()
    states: dict[str, Counter[str]] = {c: Counter() for c in channels}
    for row in good:
        for channel in channels:
            n = channel.removeprefix("CH")
            for column in ("gas", "label_source", "state", "valid"):
                if row.get(f"ch{n}_{column}") is None:
                    missing[f"ch{n}_{column}"] += 1
            if channel in asserted and (
                row.get(f"ch{n}_gas") != asserted[channel]
                or row.get(f"ch{n}_label_source") != "asserted"
            ):
                missing[f"ch{n}_asserted"] += 1
            states[channel][str(row.get(f"ch{n}_state"))] += 1
    record(
        "status and provenance retention",
        not missing,
        missing=dict(missing),
        states={c: dict(s) for c, s in states.items()},
    )
    if args.rss:
        growth = memory_growth(Path(args.rss), after_s=args.settle_s)
        record(
            "bounded memory",
            growth is not None and growth["growth_mb"] < args.max_growth_mb,
            **(growth or {"note": "too few samples after the settling time"}),
        )
    results["ok"] = all(c["ok"] for c in checks.values())
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data", help="the fuji-capture file (.csv or .parquet)")
    parser.add_argument("--rss", help="the soak_monitor.py JSON-lines log")
    parser.add_argument("--settle-s", type=float, default=3600.0, help="ignore memory before this")
    parser.add_argument("--max-growth-mb", type=float, default=20.0)
    args = parser.parse_args(argv)
    results = check(args)
    for name, outcome in results["checks"].items():
        detail = {k: v for k, v in outcome.items() if k != "ok"}
        print(f"{'PASS' if outcome['ok'] else 'FAIL'}  {name}: {json.dumps(detail, default=str)}")
    out = Path(args.data + ".check.json")
    out.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"{'all checks pass' if results['ok'] else 'some checks FAIL'}; written to {out}")
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
