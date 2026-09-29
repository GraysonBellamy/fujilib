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
  ``--max-growth-mb``, judged by the straight line fitted to every sample
  after the first hour. Two samples alone would mislead: memory rises and
  falls by tens of MB as the Parquet sink gathers and writes each row group.
  Private memory is judged when the log has it, resident memory otherwise.

A recording that was killed (its ``.meta.json`` still says ``recording``)
fails the shutdown check, and its counters are those of the last minute's
checkpoint, so the row counts are reported but not judged. Recover a killed
Parquet file with ``scripts/recover_parquet.py`` and check the recovered file.

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


def trend(points: list[tuple[float, float]], *, after_s: float) -> dict[str, float] | None:
    """The line fitted to ``(elapsed_s, mb)`` samples after ``after_s``, and what it implies."""
    settled = [p for p in points if p[0] >= after_s]
    if len(settled) < 3:  # a line through two points is not a trend
        return None
    xs, ys = [p[0] / 3600 for p in settled], [p[1] for p in settled]
    slope = statistics.linear_regression(xs, ys).slope
    return {
        "slope_mb_per_h": round(slope, 4),
        "growth_mb": round(slope * (xs[-1] - xs[0]), 3),
        "hours": round(xs[-1] - xs[0], 2),
        "first_mb": points[0][1],
        "max_mb": max(p[1] for p in points),
        "last_mb": ys[-1],
    }


def memory_growth(path: Path, *, after_s: float) -> dict[str, Any] | None:
    samples = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    samples = [s for s in samples if s.get("event") == "sample"]
    judged = "private_mb" if samples and all("private_mb" in s for s in samples) else "rss_mb"
    fits = {
        key: trend([(s["elapsed_s"], s[key]) for s in samples], after_s=after_s)
        for key in ("private_mb", "rss_mb")
        if samples and all(key in s for s in samples)
    }
    if fits.get(judged) is None:
        return None
    return {"samples": len(samples), "judged": judged, **fits[judged], "fits": fits}


def provenance(
    rows: list[dict[str, Any]], channels: list[str], asserted: dict[str, str]
) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """Successful rows missing a status column or an asserted label, and each state's count."""
    missing: Counter[str] = Counter()
    states: dict[str, Counter[str]] = {c: Counter() for c in channels}
    for row in (r for r in rows if r["error_type"] is None):
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
    return missing, states


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

    def record(name: str, ok: bool | None, **detail: Any) -> None:
        """``ok`` is None for a check that cannot be judged (SKIP)."""
        checks[name] = {"ok": None if ok is None else bool(ok), **detail}

    killed = meta["state"] == "recording"
    if "recovered" in meta:
        results["recovered"] = meta["recovered"]
    expected = [c.name for c in row_columns(ChannelId(c) for c in channels)]
    found = list(rows[0]) if rows else []
    record("readable output", bool(rows) and found == expected, columns=len(found))
    record(
        "clean shutdown",
        meta["state"] in {"finished", "stopped"},
        state=meta["state"],
        error=meta.get("error"),
        **({"note": "the process was killed; it never finished"} if killed else {}),
    )
    polls, late, dropped = summary.get("polls"), summary.get("late"), summary.get("dropped")
    target = summary.get("target_polls")
    counted = (polls or 0) + (late or 0) + (dropped or 0)
    counts = {"rows": len(rows), "polls": polls, "late": late, "dropped": dropped, "target": target}
    if killed:
        at = meta.get("updated_at") if meta.get("summary") else None
        note = f"not judged: counters of {at}" if at else "not judged: no counters were written"
        record("tick and row counts", None, **counts, note=note)
    else:
        stopped = meta["state"] == "stopped"
        record(
            "tick and row counts",
            len(rows) == polls and (target is None or stopped or counted == target),
            **counts,
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
        None
        if killed
        else len(errors) == failed_polls or (bool(dropped) and len(errors) <= failed_polls),
        error_rows=len(errors),
        failed_polls=summary.get("failed_polls"),
        types=dict(Counter(r["error_type"] for r in errors)),
        disconnects=summary.get("disconnects"),
        reconnects=summary.get("reconnects"),
        traffic=summary.get("traffic"),
    )
    missing, states = provenance(rows, channels, asserted)
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
    results["ok"] = all(c["ok"] is not False for c in checks.values())
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
        verdict = {True: "PASS", False: "FAIL", None: "SKIP"}[outcome["ok"]]
        print(f"{verdict}  {name}: {json.dumps(detail, default=str)}")
    out = Path(args.data + ".check.json")
    out.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"{'all checks pass' if results['ok'] else 'some checks FAIL'}; written to {out}")
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
