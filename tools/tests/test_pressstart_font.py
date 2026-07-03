"""Structural checks on the generated Press Start 2P panel face
(firmware/src/pressstart_font.h). Guards the invariants the firmware relies on
so a regeneration can't silently break the panel."""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PS2P = REPO / "firmware/src/pressstart_font.h"
BOLD = REPO / "firmware/src/bold_font.h"

PANEL_COLS = 32  # four 8x8 modules
GAP = 1  # library inter-glyph spacing


def parse(path: Path, array: str):
    """Return (header, {code: [columns]}) for a committed MD_MAX72XX font header."""
    body = path.read_text().split(f"{array}[] PROGMEM = {{", 1)[1].split("};", 1)[0]
    vals = [int(t, 16) for t in re.findall(r"0x[0-9a-fA-F]+", body)]
    header = vals[:7]
    first = (header[2] << 8) | header[3]
    glyphs, i, code = {}, 7, first
    while i < len(vals):
        w = vals[i]
        i += 1
        glyphs[code] = vals[i:i + w]
        i += w
        code += 1
    return header, glyphs


@pytest.fixture(scope="module")
def font():
    return parse(PS2P, "PRESSSTART_FONT")


@pytest.fixture(scope="module")
def bold():
    return parse(BOLD, "BOLD_FONT")


def test_valid_v2_header_height_8(font):
    header, _ = font
    assert header[:2] == [ord("F"), 2], "expected v2 'F',2 magic"
    assert header[6] == 8, "panel is 8 rows tall"


def test_same_code_range_as_bold(font, bold):
    """PS2P must be a drop-in over bold's range so no steady glyph goes blank."""
    fh, fg = font
    bh, bg = bold
    assert fh[2:6] == bh[2:6], "code range must match the bold face"
    assert set(fg) == set(bg)


def test_lowercase_folds_onto_uppercase(font):
    _, g = font
    for c in range(ord("a"), ord("z") + 1):
        assert g[c] == g[c - 0x20], f"{chr(c)!r} should render its uppercase glyph"


def test_digits_are_tabular(font):
    _, g = font
    widths = {len(g[c]) for c in range(ord("0"), ord("9") + 1)}
    assert len(widths) == 1, f"digits must share one width for jitter-free updates, got {widths}"


def test_glyph_widths(font):
    """Caps are up to 7px (2px strokes); digits are hand-condensed to 6px so a
    five-glyph time fits the panel."""
    _, g = font
    for c in range(ord("A"), ord("Z") + 1):
        assert 0 < len(g[c]) <= 7, f"{chr(c)!r} width {len(g[c])}, expected 1..7"
    assert len(g[ord("W")]) == 7, "the widest caps must reach the full 7px cell"
    for c in range(ord("0"), ord("9") + 1):
        assert len(g[c]) == 6, f"digit {chr(c)!r} width {len(g[c])}, expected condensed 6"


def test_four_caps_fit_the_panel(font):
    """Sign text is capped at 4 chars; 4 caps + 3 gaps must fit 32 columns."""
    _, g = font
    w = len(g[ord("W")])
    assert 4 * w + 3 * GAP <= PANEL_COLS


def test_five_glyph_number_fits(font):
    """The condensed 6px digits must let a five-glyph time (e.g. "12:00") fit the
    panel, so the clock/timer can stay in the Press Start 2P face."""
    _, g = font
    d = len(g[ord("0")])
    colon = len(g[ord(":")])
    width = 4 * d + colon + 4 * GAP  # e.g. "12:00"
    assert width <= PANEL_COLS, f"five-glyph time is {width}px, must fit {PANEL_COLS}"


def test_colon_is_two_narrow_columns(font):
    _, g = font
    assert len(g[ord(":")]) == 2, "colon should be a narrow 2px glyph"
    assert all(b for b in g[ord(":")]), "both colon columns carry ink"


def test_fallback_glyphs_match_bold(font, bold):
    """Codes PS2P lacks (stock arrows, degree) fall back to the bold glyph."""
    _, fg = font
    _, bg = bold
    for code in (0x18, 0x19, 0xB0):  # up arrow, down arrow, degree
        assert fg[code] == bg[code], f"0x{code:02x} should fall back to bold"


def test_sentinel_glyphs_have_ink(font):
    _, g = font
    for ch in "A0M8%":
        assert any(g[ord(ch)]), f"{ch!r} unexpectedly blank"
