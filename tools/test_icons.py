"""
Tests for the icon generator, and for the three files that draw the same mark.

The artwork exists in three places that cannot import each other: the React
component the dashboard renders, the SVG favicon the browser loads, and the
coordinates ``make_icons.py`` rasterises from. Nothing but a test can keep them
in agreement, and "the logo is subtly wrong in one of the four places it
appears" is exactly the kind of thing nobody notices for a release or two.

Deliberately stdlib-only. Pillow is needed to *generate* the icons and is not a
project dependency — it is installed when someone regenerates them — so these
read PNG and ICO headers by hand rather than skipping when it is absent. A
guard that quietly skips is the failure mode this repository already has a rule
about.
"""

from __future__ import annotations

import re
import struct
import xml.dom.minidom
from pathlib import Path

import pytest

from make_icons import ICO_SIZES, INTERIOR, PNG_SIZES, RECTS, SAFE_ZONE, WEDGE

ROOT = Path(__file__).resolve().parent.parent
WORDMARK = ROOT / "frontend" / "src" / "ui" / "Wordmark.tsx"
FAVICON = ROOT / "frontend" / "public" / "favicon.svg"
ICONS_DIR = ROOT / "frontend" / "public" / "icons"
ICO = ROOT / "frontend" / "public" / "favicon.ico"
INSTALLER_ICO = ROOT / "packaging" / "windows" / "kurukuru.ico"
ISS = ROOT / "packaging" / "windows" / "kurukuru.iss"

RECT_RE = re.compile(
    r'<rect\s+x="(-?\d+)"\s+y="(-?\d+)"\s+width="(\d+)"\s+height="(\d+)"'
)
PATH_RE = re.compile(r'd="M(\d+) (\d+) h(\d+) l(\d+) (\d+) h-(\d+) Z"')


def rects_in(text: str) -> list[tuple[int, int, int, int]]:
    return [tuple(int(g) for g in m.groups()) for m in RECT_RE.finditer(text)]


def wedge_in(text: str) -> list[tuple[int, int]]:
    """The wedge, resolved from its relative path data to absolute points."""
    m = PATH_RE.search(text)
    assert m, "no wedge path found"
    x, y, h1, dx, dy, back = (int(g) for g in m.groups())
    return [(x, y), (x + h1, y), (x + h1 + dx, y + dy), (x + h1 + dx - back, y + dy)]


# --------------------------------------------------------------------------- #
# The three copies of the artwork agree
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source", [WORDMARK, FAVICON], ids=["component", "favicon"])
def test_icon_geometry(source: Path) -> None:
    """Every drawing of the mark uses the generator's coordinates exactly."""
    text = source.read_text(encoding="utf-8")
    assert rects_in(text) == RECTS, f"{source.name} rectangles have drifted"
    assert wedge_in(text) == WEDGE, f"{source.name} wedge has drifted"


@pytest.mark.parametrize("source", [WORDMARK, FAVICON], ids=["component", "favicon"])
def test_safe_zone_is_the_same_translation_everywhere(source: Path) -> None:
    """The 12% safe zone is applied, and applied as the same offset."""
    offset = int(SAFE_ZONE * (INTERIOR / (1 - 2 * SAFE_ZONE)))
    assert f"translate({offset}, {offset})" in source.read_text(encoding="utf-8")


def test_favicon_is_well_formed_xml() -> None:
    """A favicon that does not parse renders as a broken image and says nothing.

    This has already happened once: a comment in the file mentioned two CSS
    custom properties by name, the leading double hyphens are illegal inside an
    XML comment, and the only symptom was a broken-image glyph in the tab.
    """
    xml.dom.minidom.parse(str(FAVICON))


def test_favicon_carries_both_themes() -> None:
    """It cannot inherit a colour, so it must bring one for each scheme."""
    text = FAVICON.read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in text
    assert text.count("fill:") >= 2, "expected a light fill and a dark override"


# --------------------------------------------------------------------------- #
# The generated files are present and are what they claim
# --------------------------------------------------------------------------- #
def png_size(path: Path) -> tuple[int, int]:
    """Width and height from the IHDR chunk."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def ico_sizes(path: Path) -> set[int]:
    """The sizes in an ICO directory. 0 in the header means 256."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, f"{path.name} is not an ICO"
    sizes = set()
    for i in range(count):
        entry = 6 + i * 16
        width = data[entry] or 256
        height = data[entry + 1] or 256
        assert width == height, f"{path.name} has a non-square {width}x{height} frame"
        sizes.add(width)
    return sizes


def test_every_png_size_exists_and_is_that_size() -> None:
    for size in PNG_SIZES:
        path = ICONS_DIR / f"icon-{size}.png"
        assert path.exists(), f"{path} missing — run tools/make_icons.py"
        assert png_size(path) == (size, size)


def test_ico_carries_every_size_windows_asks_for() -> None:
    """Including 16, which is the one a downscale ruins on this artwork."""
    assert ICO.exists(), "favicon.ico missing — run tools/make_icons.py"
    assert ico_sizes(ICO) == set(ICO_SIZES)
    assert 16 in ico_sizes(ICO)


def test_installer_ico_is_the_same_file() -> None:
    """Inno needs it beside the .iss; it must not become a second artwork."""
    assert INSTALLER_ICO.exists(), "packaging icon missing — run tools/make_icons.py"
    assert INSTALLER_ICO.read_bytes() == ICO.read_bytes()


def test_installer_actually_uses_it() -> None:
    """The generated icon is worth nothing if the script still ships the default."""
    text = ISS.read_text(encoding="utf-8")
    assert "SetupIconFile=kurukuru.ico" in text
    assert r"UninstallDisplayIcon={app}\kurukuru.ico" in text
    assert 'Source: "kurukuru.ico"' in text, "the .ico is never installed"


def test_raster_ink_reads_on_both_themes() -> None:
    """The one ink has to clear 3:1 against a light and a dark surface.

    Rasters cannot follow a theme, and the first attempt at this shipped a
    near-black mark that measured 1.15:1 on the dark surface — invisible, and
    only caught by looking at it.
    """
    from make_icons import INK

    def luminance(rgb: tuple[int, ...]) -> float:
        channels = []
        for value in rgb[:3]:
            c = value / 255
            channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
        r, g, b = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(a: tuple[int, ...], b: tuple[int, ...]) -> float:
        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for surface, name in [((250, 250, 250), "light"), ((8, 8, 10), "dark"), ((32, 32, 32), "explorer dark")]:
        ratio = contrast(INK, surface)
        assert ratio >= 3.0, f"ink is {ratio:.2f}:1 on the {name} surface, below 3:1"
