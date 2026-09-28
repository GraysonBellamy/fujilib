"""Extract searchable text from the vendor manuals in docs/manuals/ (design §11).

Writes ``<name>.txt`` beside each PDF, with ``===== PAGE n =====`` markers that
match the PDF page numbers. ``<name>`` is the PDF's stem without its download
suffix (``TN2ZPAb-E_gxquyffkou.pdf`` -> ``TN2ZPAb-E.txt``).

Some spans in the manuals are drawn with embedded subset fonts that have no
usable Unicode mapping, so a plain extraction returns glyph IDs instead of
characters: the "shifted" text, such as ``7KH`` for ``The``. This script
repairs those characters per font, using only evidence:

1. **Learned.** The same font usually also appears with a working mapping
   elsewhere in the document. Glyph ID -> character pairs are learned from
   those spans. If any glyph ID maps to two characters (subsets that renumber
   their glyphs), the font's learned pairs are not used at all.
2. **Offset.** In the Microsoft core fonts, printable ASCII sits at a constant
   glyph-ID offset (+29 in Arial and Times New Roman). The offset is inferred
   from the learned pairs, or taken from ``KNOWN_OFFSETS`` for a font that never
   appears correctly mapped.
3. **Known glyphs.** A few ligatures, confirmed from the surrounding words.

Anything else becomes U+FFFD rather than a guess. The script also rebuilds each
page's layout itself; if that does not reproduce PyMuPDF's own plain-text
output for the page, the page is written unrepaired and reported.

PyMuPDF is not a project dependency. Run with::

    uv run --no-project --with pymupdf python scripts/extract_manuals.py

``--check`` compares against the existing extracts instead of writing them.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

REPLACEMENT = "�"
PAGE_MARKER = "\n\n===== PAGE {n} =====\n"
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "docs" / "manuals"

# PyMuPDF's plain-text flags. The CID flag makes an unmapped glyph come out as
# its glyph ID (the value this script decodes) instead of U+FFFD.
FLAGS_WITH_CID = pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE

# Glyph-ID offsets for fonts that never appear with a working mapping, each
# checked by hand against the words it produces.
KNOWN_OFFSETS: dict[str, int] = {
    # TN5A1191b-E, the service manual's screen shots (§3):
    # "(CEVQT[\x02/QFG" -> "Factory Mode", "2TQVQEQN" -> "Protocol".
    "MS-Gothic-90ms-RKSJ-H": 30,
    # TN2ZPAb-E pp. 51, 53, 59, 88, the menu diagrams: "0EAK\x00!LARM" ->
    # "Peak Alarm", "/.\x0f/&&" -> "ON/OFF". Its correctly mapped spans come
    # from other subsets with other numbering, so the offset cannot be learned.
    "Times-Roman": 32,
}

# Glyphs no correctly mapped span covers, each identified from the words around
# it (page numbers are PDF pages). List markers of any shape become "•".
_CORE_FONT_GLYPHS = {  # Arial and Times New Roman share these positions
    0x93: "±",  # TN2ZPA p21 "0.5 L/min ± 0.2 L/min"
    0x94: "≤",  # TN2ZPA p94 "≤ 1.0% FS"; TN5A1191 p33 "10,000 ≤ No. 17–20 ≤ 59,999"
    0x9F: "Ω",  # TN2ZPA p27 "4 to 20 mA DC, 550Ω or less"; TN5A1191 p19
    0xB6: "’",  # noqa: RUF001 - right single quote: TN2ZPA p92 "Fuji's Zirconia"
    0xBF: "fi",  # TN2ZPA p2 "specifications", "modification"
    0xC0: "fl",  # TN2ZPA "flow", "flowmeter", "Teflon"
    0xDB: "°",  # TN2ZPA p23 "within 0 to 50°C", p92 "-5°C to 45°C"
}
_TIMES_LIGATURES = {
    0xD75: "ff",  # TN2ZPA p5 "turn off", "different"; p86 "rub off"
    0xD76: "ffi",  # TN2ZPA p7 "insufficient"; p10 "offices"; p11 "difficult"
}
KNOWN_GLYPHS: dict[str, dict[int, str]] = {
    "TimesNewRomanPSMT": _CORE_FONT_GLYPHS | _TIMES_LIGATURES,
    "TimesNewRomanPS-BoldMT": _CORE_FONT_GLYPHS | _TIMES_LIGATURES,
    "ArialMT": _CORE_FONT_GLYPHS | {0x1087: "ff"},  # TN2ZPA p92-93 "effective"
    "Arial-BoldMT": _CORE_FONT_GLYPHS,
    "SymbolMT": {  # TN2ZPA
        0x03: " ",
        0x3A: "Ω",  # p28 "DC input resistor of 1MΩ or more"
        0x50: "μ",  # p23 "dust particles of 0.3μm"
        0x6F: "→",  # p41 "< User mode > → < Calibration parameters >"
        0x78: "•",  # p20 list markers
    },
    "Symbol": {  # TN5A1191
        0x64: "≤",  # p33 "0.5 ≤ zero calibration coefficient ≤ 5"
        0x72: "±",  # p23 "+5 ±0.3 V", "+15 ±0.5 V"
        0x78: "•",  # p2 list markers
        0x9F: "•",  # p35 list markers
    },
    "Wingdings-Regular": {0x7A: "•"},  # TN5A1191 p19 list markers
    "Times-Roman": {0x73: "•"},  # TN2ZPA p51 list markers ("• Peak Alarm")
}

# An offset is inferred only from enough letter pairs that mostly agree.
MIN_OFFSET_SAMPLES = 20
MIN_OFFSET_AGREEMENT = 0.9


@dataclass
class FontRepair:
    """Per-font decoding evidence gathered from one PDF."""

    learned: dict[str, dict[int, str]] = field(default_factory=dict)
    conflicted: set[str] = field(default_factory=set)
    offsets: dict[str, int] = field(default_factory=dict)

    def decode(self, font: str, gid: int) -> tuple[str, str]:
        """Return ``(text, method)`` for glyph ``gid`` of ``font``."""
        if font not in self.conflicted and gid in self.learned.get(font, {}):
            return self.learned[font][gid], "learned"
        if gid in KNOWN_GLYPHS.get(font, {}):
            return KNOWN_GLYPHS[font][gid], "known"
        offset = self.offsets.get(font)
        if offset is not None and 0x20 <= gid + offset <= 0x7E:
            return chr(gid + offset), "offset"
        return REPLACEMENT, "unresolved"


def learn(doc: pymupdf.Document) -> FontRepair:
    """Learn glyph mappings and offsets from the correctly mapped spans."""
    seen: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for page in doc:
        for span in page.get_texttrace():
            for ucs, gid, *_ in span["chars"]:
                if gid > 0 and ucs >= 0x20 and chr(ucs) != REPLACEMENT:
                    seen[span["font"]][gid].add(chr(ucs))

    repair = FontRepair()
    for font, glyphs in seen.items():
        if any(len(chars) > 1 for chars in glyphs.values()):
            repair.conflicted.add(font)
        repair.learned[font] = {gid: next(iter(c)) for gid, c in glyphs.items() if len(c) == 1}
        diffs = Counter(
            ord(ch) - gid
            for gid, ch in repair.learned[font].items()
            if ch.isascii() and ch.isalpha()
        )
        total = sum(diffs.values())
        if total >= MIN_OFFSET_SAMPLES:
            offset, count = diffs.most_common(1)[0]
            if count / total >= MIN_OFFSET_AGREEMENT:
                repair.offsets[font] = offset
    for font, offset in KNOWN_OFFSETS.items():
        repair.offsets.setdefault(font, offset)
    return repair


def _render(lines: list[str]) -> str:
    # PyMuPDF's plain-text writer ends a line with "\n" unless its last
    # character is NUL or already a newline. For a repaired line this is decided
    # on the repaired text: a glyph ID that happened to be 0 or 10 is not a real
    # NUL or newline.
    return "".join(
        text + "\n" if text and text[-1] not in ("\x00", "\n") else text for text in lines
    )


def _origin_key(origin: tuple[float, float]) -> tuple[float, float]:
    return round(origin[0], 3), round(origin[1], 3)


def page_text(
    page: pymupdf.Page, repair: FontRepair, stats: Counter[tuple[str, str]]
) -> str | None:
    """Return the repaired text of ``page``, or ``None`` if its layout cannot be rebuilt."""
    # Unmapped glyphs, located by where they are drawn. (A second rawdict pass
    # without the CID flag would mark them too, but MuPDF then assembles lines
    # differently, so the two passes cannot be aligned.)
    unmapped: dict[tuple[float, float], tuple[str, int]] = {}
    for span in page.get_texttrace():
        for ucs, gid, origin, _bbox in span["chars"]:
            if chr(ucs) == REPLACEMENT and gid >= 0:
                unmapped[_origin_key(origin)] = (span["font"], gid)

    raw_lines: list[str] = []
    fixed_lines: list[str] = []
    for block in page.get_text("rawdict", flags=FLAGS_WITH_CID)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            raw: list[str] = []
            fixed: list[str] = []
            for span in line["spans"]:
                for char in span["chars"]:
                    raw.append(char["c"])
                    hit = unmapped.get(_origin_key(char["origin"]))
                    if hit is None or hit[1] != ord(char["c"]):
                        fixed.append(char["c"])
                        continue
                    font, gid = hit
                    text, method = repair.decode(font, gid)
                    stats[font, method] += 1
                    if method == "unresolved":
                        stats[font, f"unresolved gid {gid:#x}"] += 1
                    fixed.append(text)
            raw_lines.append("".join(raw))
            fixed_lines.append("".join(fixed))
    # Self-check: without repairs, the rebuilt page must equal PyMuPDF's own output.
    if _render(raw_lines) != page.get_text("text", flags=FLAGS_WITH_CID):
        return None
    return _render(fixed_lines)


def extract(pdf: Path) -> tuple[str, Counter[tuple[str, str]], list[int]]:
    """Return the repaired text of ``pdf``, the repair statistics and any unrepaired pages."""
    stats: Counter[tuple[str, str]] = Counter()
    fallback_pages: list[int] = []
    parts: list[str] = []
    with pymupdf.open(pdf) as doc:
        repair = learn(doc)
        for page in doc:
            text = page_text(page, repair, stats)
            if text is None:
                fallback_pages.append(page.number + 1)
                text = page.get_text("text", flags=FLAGS_WITH_CID)
            parts.append(PAGE_MARKER.format(n=page.number + 1) + text)
    return "".join(parts), stats, fallback_pages


def output_path(pdf: Path) -> Path:
    """``TN2ZPAb-E_gxquyffkou.pdf`` -> ``TN2ZPAb-E.txt`` beside it."""
    stem = pdf.stem.rsplit("_", 1)[0] if "_" in pdf.stem else pdf.stem
    return pdf.with_name(f"{stem}.txt")


def report(pdf: Path, stats: Counter[tuple[str, str]], fallback_pages: list[int]) -> None:
    print(f"{pdf.name}")
    fonts = sorted({font for font, _ in stats})
    for font in fonts:
        methods = {
            m: n for (f, m), n in stats.items() if f == font and not m.startswith("unresolved gid")
        }
        unresolved = sorted(
            m.removeprefix("unresolved gid ")
            for (f, m), _ in stats.items()
            if f == font and m.startswith("unresolved gid")
        )
        line = ", ".join(f"{m} {n}" for m, n in sorted(methods.items()))
        if unresolved:
            line += f"  (unresolved glyphs: {' '.join(unresolved)})"
        print(f"  {font}: {line}")
    if fallback_pages:
        print(f"  pages written unrepaired (layout self-check failed): {fallback_pages}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--manuals-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the existing .txt files instead of writing them; exit 1 if any differ",
    )
    args = parser.parse_args(argv)

    pdfs = sorted(args.manuals_dir.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs in {args.manuals_dir}", file=sys.stderr)
        return 1
    differs = False
    for pdf in pdfs:
        text, stats, fallback_pages = extract(pdf)
        report(pdf, stats, fallback_pages)
        out = output_path(pdf)
        if args.check:
            current = out.read_text(encoding="utf-8") if out.exists() else None
            same = current == text
            differs |= not same
            print(f"  {out.name}: {'up to date' if same else 'differs'}")
        else:
            out.write_text(text, encoding="utf-8", newline="\n")
            print(f"  wrote {out.name}")
    return 1 if differs else 0


if __name__ == "__main__":
    raise SystemExit(main())
