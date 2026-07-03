#!/usr/bin/env python3
"""Generate the static panel face (firmware/src/pressstart_font.h) from the real
Press Start 2P TTF.

Press Start 2P is a blocky arcade font laid out on a clean 8-row grid: at
font-size 8*S the em is exactly 8*S device pixels tall, so one font-pixel maps
to S device pixels with no fractional edges. We rasterize each glyph at S=10
with antialiasing OFF and sample the centre of every design cell — lossless, so
the result is pixel-perfect to the original.

Two panel rules match the bold face (see gen_bold_font.py):

1. Fold a-z onto A-Z. Each lowercase code emits its uppercase glyph, so the
   panel renders all-caps whatever the input case — the panel's identity, and
   Press Start 2P's lowercase has descenders that don't fit 8 rows anyway.
2. Tabular digits. The digits are hand-condensed to 6px (DIGIT_ART) so five
   glyphs fit the 32-col panel — the original 7px digits overflow (clock HH:MM,
   timer MM:SS). They share one width, so in-place numeric updates don't jitter.

Codes the TTF has no glyph for — the control range (including the 0x18/0x19
stock-movement arrows) and high symbols like degree (0xB0) — fall back to the
committed BOLD_FONT glyph. So this table is a drop-in over the same 0x00..0xFF
range as the bold face and no steady glyph can go blank. Those codes only reach
the panel through the scrolling system font anyway; the fallback is belt-and-
braces.

Glyph widths are stored ink-only (the library adds a 1px inter-glyph gap at
render time). Caps are 7px; the condensed digits are 6px, so a five-glyph time
(4 digits + a 2px colon + four 1px gaps = 30 cols) fits the 32-col panel.

Font-table format (MD_MAX72XX v2):
    'F', 2, firstHi, firstLo, lastHi, lastLo, height
  then per glyph: [width][col0]..[colN]   # each col byte, bit 0 = top row

Usage:
  uv run --extra dev python tools/gen_pressstart_font.py
"""
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
TTF = REPO / "tools/fonts/PressStart2P-Regular.ttf"
BOLD_HEADER = REPO / "firmware/src/bold_font.h"
OUT = REPO / "firmware/src/pressstart_font.h"

HEADER_LEN = 7  # 'F', version=2, firstHi, firstLo, lastHi, lastLo, height
SCALE = 10  # device pixels per design cell
EM_ROWS = 8  # Press Start 2P design grid height
PX = EM_ROWS * SCALE  # font size that maps 1 design cell -> SCALE device px
ASCII_LO, ASCII_HI = 0x20, 0x7E  # printable range the TTF covers


# Condensed 6px digits. Press Start 2P's own digits are 7px, so five glyphs
# (clock HH:MM, timer MM:SS) overflow the 32-col panel and squish if the gap is
# dropped. These hand-drawn 6px versions keep the face's character — the 1's
# flag and serif base, the 4's notch, the 7's slant, the angular 2/5 — but fit:
# four 6px digits + a 2px colon + four 1px gaps = 30 columns. Applied to 0-9
# globally so the whole panel (clock, timer, signs with digits) stays one face.
# Rows top-to-bottom; '#' = lit. See tools/fonts/pressstart_preview.txt.
DIGIT_ART = {
    "0": [".####.", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
    "1": ["..##..", ".###..", "..##..", "..##..", "..##..", "..##..", "######"],
    "2": [".####.", "##..##", "...##.", "..##..", ".##...", "##....", "######"],
    "3": [".####.", "##..##", "...##.", "..###.", "....##", "##..##", ".####."],
    "4": ["...##.", "..###.", ".#.##.", "##.##.", "######", "...##.", "...##."],
    "5": ["######", "##....", "#####.", "....##", "....##", "##..##", ".####."],
    "6": ["..###.", ".##...", "##....", "#####.", "##..##", "##..##", ".####."],
    "7": ["######", "##..##", "...##.", "..##..", "..##..", ".##...", ".##..."],
    "8": [".####.", "##..##", "##..##", ".####.", "##..##", "##..##", ".####."],
    "9": [".####.", "##..##", "##..##", ".#####", "....##", "...##.", ".###.."],
}


def art_cols(rows: list[str]) -> list[int]:
    """Convert 7-row ASCII art into ink columns (bit 0 = top row)."""
    width = len(rows[0])
    return [sum((1 << r) for r in range(len(rows)) if rows[r][c] == "#")
            for c in range(width)]


def parse_bold(src: str) -> tuple[list[int], dict[int, list[int]]]:
    """Parse bold_font.h into (header, {code: columns}). Used as the fallback
    base table so codes Press Start 2P lacks keep a real glyph."""
    m = re.search(r"BOLD_FONT\[\]\s*PROGMEM\s*=\s*\{", src)
    if not m:
        sys.exit("could not find BOLD_FONT[] in bold_font.h")
    body = src[m.end():].split("};", 1)[0]
    vals = [int(tok, 16) for tok in re.findall(r"0x[0-9a-fA-F]+", body)]
    if vals[:2] != [ord("F"), 2]:
        sys.exit(f"unexpected bold header {vals[:HEADER_LEN]} — expected v2")
    first = (vals[2] << 8) | vals[3]
    glyphs: dict[int, list[int]] = {}
    i, code = HEADER_LEN, first
    while i < len(vals):
        width = vals[i]
        i += 1
        glyphs[code] = vals[i:i + width]
        i += width
        code += 1
    return vals[:HEADER_LEN], glyphs


def raster_cols(font: ImageFont.FreeTypeFont, baseline_y: int, ch: str) -> tuple[list[int], bool]:
    """Rasterize one glyph and downsample it to the design grid. Returns (ink
    columns, clipped) where each column is a byte with bit 0 = top em row, and
    clipped is True if ink falls below the baseline (a descender the 8-row em
    can't hold). Empty ink returns ([], False).

    The sample grid is anchored to the pen origin (a multiple of SCALE), which
    is where Press Start 2P's design pixels sit — anchoring to the per-glyph ink
    bbox instead would misalign beveled/diagonal glyphs (the digits). Each design
    cell is decided by majority of its SCALE*SCALE block, so partial-coverage
    edges resolve cleanly."""
    pen_x = PX  # multiple of SCALE, so the design grid lands on cell boundaries
    img = Image.new("L", (PX * 4, PX * 2), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # no antialiasing — crisp block edges
    d.text((pen_x, baseline_y), ch, fill=255, font=font, anchor="ls")  # left/baseline
    bbox = img.getbbox()
    if bbox is None:
        return [], False
    px = img.load()
    em_top = baseline_y - PX  # top of the 8-row em box
    max_cols = (img.width - pen_x) // SCALE
    raw: list[int] = []
    for c in range(max_cols):
        byte = 0
        for r in range(EM_ROWS):
            lit = sum(
                px[pen_x + c * SCALE + dx, em_top + r * SCALE + dy] > 127
                for dy in range(SCALE) for dx in range(SCALE)
            )
            if lit * 2 > SCALE * SCALE:  # majority of the cell is ink
                byte |= 1 << r
        raw.append(byte)
    # trim leading/trailing empty columns to store ink-only width
    while raw and raw[0] == 0:
        raw.pop(0)
    while raw and raw[-1] == 0:
        raw.pop()
    return raw, bbox[3] > baseline_y


def build() -> list[int]:
    header, base = parse_bold(BOLD_HEADER.read_text())
    first = (header[2] << 8) | header[3]
    last = (header[4] << 8) | header[5]
    font = ImageFont.truetype(str(TTF), PX)
    ascent, _ = font.getmetrics()
    if ascent != PX:
        sys.exit(f"unexpected ascent {ascent}, expected {PX} (em not 8 rows?)")

    # Rasterize the printable range; fold a-z onto A-Z. Empty ink keeps the base
    # (bold) glyph so space and any TTF gap stay sensible.
    ps: dict[int, list[int]] = {}
    for code in range(ASCII_LO, ASCII_HI + 1):
        src = code - 0x20 if ord("a") <= code <= ord("z") else code  # fold
        cols, clipped = raster_cols(font, ascent, chr(src))
        if not cols:
            continue  # space / no ink -> fall through to base
        if clipped:
            print(f"warning: glyph {chr(src)!r} (0x{code:02x}) descends below the "
                  f"baseline — tail clipped to 8 rows", file=sys.stderr)
        ps[code] = cols

    for d, rows in DIGIT_ART.items():  # swap in the condensed 6px digits
        ps[ord(d)] = art_cols(rows)

    glyphs = {code: ps.get(code, base.get(code, [])) for code in range(first, last + 1)}

    # Tabular digits: pad every 0-9 glyph (centred) to the widest digit.
    digits = [glyphs[c] for c in range(ord("0"), ord("9") + 1) if glyphs.get(c)]
    dw = max((len(g) for g in digits), default=0)
    for c in range(ord("0"), ord("9") + 1):
        g = glyphs.get(c)
        if g and len(g) < dw:
            pad = dw - len(g)
            glyphs[c] = [0] * (pad // 2) + g + [0] * (pad - pad // 2)

    out = list(header)
    for code in range(first, last + 1):
        g = glyphs[code]
        out.append(len(g))
        out.extend(g)
    return out


def render(vals: list[int]) -> str:
    lines = [
        "  " + ", ".join(f"0x{b:02x}" for b in vals[k:k + 16]) + ","
        for k in range(0, len(vals), 16)
    ]
    body = "\n".join(lines)
    return f"""// Generated by tools/gen_pressstart_font.py — do not edit by hand.
// The panel's static face: Press Start 2P, rasterized pixel-perfect from the
// original TTF onto the 8-row grid, and used for everything steady.
//   - a-z fold onto A-Z, so the panel renders all-caps (PS2P lowercase has
//     descenders that don't fit 8 rows).
//   - Caps are 7px wide (2px strokes). Digits are hand-condensed to 6px so five
//     glyphs fit the 32-col panel (clock HH:MM, timer MM:SS = 4 digits + colon +
//     gaps = 30 cols); the original 7px digits would overflow or squish.
//   - Codes PS2P lacks (control range incl. the 0x18/0x19 arrows, degree 0xB0)
//     fall back to the bold glyph, so this is a drop-in over bold's code range.
// See tools/gen_pressstart_font.py. OFL license: tools/fonts/OFL.txt.
#pragma once
#include <MD_MAX72xx.h>

// const keeps this in flash-mapped .rodata on ESP32 (a non-const PROGMEM array
// would land in RAM). setFont() wants a non-const pointer but only ever reads
// the table (pgm_read_byte), so callers pass it via a const_cast.
const MD_MAX72XX::fontType_t PRESSSTART_FONT[] PROGMEM = {{
{body}
}};
"""


def preview(vals: list[int]) -> str:
    """Human-auditable ASCII of the built table, so the committed font can be
    eyeballed and diffed. Regenerated alongside the header."""
    first = (vals[2] << 8) | vals[3]
    glyphs: dict[int, list[int]] = {}
    i, code = HEADER_LEN, first
    while i < len(vals):
        w = vals[i]
        i += 1
        glyphs[code] = vals[i:i + w]
        i += w
        code += 1

    def block(s: str) -> list[str]:
        cols: list[int] = []
        for ch in s:
            cols.extend(glyphs.get(ord(ch), []))
            cols.append(0)  # 1px inter-glyph gap, as the library renders it
        return ["".join("#" if (b >> r) & 1 else "." for b in cols) for r in range(EM_ROWS)]

    lines = ["Press Start 2P panel face — generated by tools/gen_pressstart_font.py",
             "'#' = lit pixel, columns separated by the library's 1px gap.", ""]
    for s in ("ABCDEFGHIJKLM", "NOPQRSTUVWXYZ", "0123456789",
              ".,:;!?'\"()-+/", "$%&*#@<>=", "9:41  12:00"):
        lines.append(f"=== {s} ===")
        lines.extend(block(s))
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    if not TTF.exists():
        sys.exit(f"font not found: {TTF}")
    vals = build()
    OUT.write_text(render(vals))
    PREVIEW = REPO / "tools/fonts/pressstart_preview.txt"
    PREVIEW.write_text(preview(vals))
    print(f"wrote {OUT} ({len(vals)} bytes) and {PREVIEW}")


if __name__ == "__main__":
    main()
