"""Derive the committed bench bank from the local register capture (design §13.1 #11).

The raw capture (``tests/fixtures/captures/zpa_bench_*.json``) stays local: it
holds the analyzer's factory calibration and configuration blocks. This keeps
only what fujilib models and nothing else:

- FC04: the documented measurement (0000h-00C1h) and fixed-setting
  (0425h-0469h) regions, and the observed clock and A/D block (03E8h-0418h);
- FC03: the documented user settings (0000h-00ABh).

The factory blocks (FC03 03E8h-069Bh and 0BB8h-0C66h) and the unexplained FC04
words (0419h-0424h, 046Ah-0479h) are dropped. The output has the same shape
as the capture, so ``fuji-decode --dump`` reads either.

Usage:
    python scripts/sanitize_capture.py            # write the fixture
    python scripts/sanitize_capture.py --check    # exit 1 if it would change
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE = REPO_ROOT / "tests" / "fixtures" / "captures" / "zpa_bench_20260928.json"
OUTPUT = REPO_ROOT / "tests" / "fixtures" / "zpa_bench_documented.json"

KEEP = {
    "input": ((0x0000, 0x00C1), (0x0425, 0x0469), (0x03E8, 0x0418)),
    "holding": ((0x0000, 0x00AB),),
}


def sanitize(capture: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {
        "description": (
            "Documented register blocks of the bench Fuji ZPA (CO2 / CO / O2), plus the "
            "observed clock and A/D block, derived from the local capture by "
            "scripts/sanitize_capture.py. Factory calibration and configuration blocks "
            "are omitted. Assembled one word at a time over several minutes, so the "
            "blocks are not a coherent snapshot."
        ),
        "source": "scripts/sanitize_capture.py from scripts/probe_scan.py output",
    }
    for key in ("station", "serial_settings", "captured_utc", "type_code", "firmware"):
        out[key] = capture[key]
    out["regions"] = {
        table: [f"{first:04X}-{last:04X}" for first, last in spans] for table, spans in KEEP.items()
    }
    for table, spans in KEEP.items():
        bank = capture[table]
        assert isinstance(bank, dict)
        out[table] = {
            f"{a:04X}": bank[f"{a:04X}"]
            for first, last in spans
            for a in range(first, last + 1)
            if f"{a:04X}" in bank
        }
    return out


def render(capture: dict[str, object]) -> str:
    return json.dumps(sanitize(capture), indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if the fixture is stale.")
    args = parser.parse_args(argv)
    if not CAPTURE.exists():
        sys.stderr.write(f"{CAPTURE.relative_to(REPO_ROOT)} is not here; it is kept local.\n")
        return 1
    rendered = render(json.loads(CAPTURE.read_text(encoding="utf-8")))
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        return 0 if current == rendered else 1
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
