"""
Render the Kurukuru mark to the raster icons the browser and the installer need.

The mark is six axis-aligned shapes on a 100x100 grid, so it is drawn here from
those coordinates rather than rasterised from the SVG. That removes the need for
an SVG rasteriser in the toolchain entirely, and it means the icons cannot drift
from the artwork by way of some converter's idea of anti-aliasing: the geometry
below and ``frontend/src/ui/Wordmark.tsx`` are the same six numbers twice, and
``test_icon_geometry`` is what keeps them that way.

Anti-aliasing is supersampling — drawn at 8x and box-filtered down. The wedge is
the only shape with a diagonal and the only one that needs it, but drawing
everything the same way avoids one shape's edges being crisper than another's.

Why not ``currentColor`` here: a favicon has nothing to inherit from. The SVG
favicon carries a ``prefers-color-scheme`` rule so it follows the browser's
theme, but ``.ico`` and PNG have no such mechanism, so they are rendered once in
a single ink that has to work on both a light and a dark tab strip. See
DECISIONS for the colour choice.

Usage:
    python tools/make_icons.py            # writes frontend/public + packaging
    python tools/make_icons.py --check    # verifies outputs are current
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# Pillow is imported inside the functions that draw, not here.
#
# The coordinates, the ink and the size lists are the parts other code reads —
# `test_icons.py` checks them against the React component and the SVG favicon,
# and says in its own docstring that it needs no Pillow so the guards run in a
# checkout that has never installed it. That was not true while this import sat
# at module scope: importing any constant pulled in Pillow and the whole file
# failed to collect without it. Found by writing the CI workflow, which is
# exactly the kind of claim a second machine checks and a familiar one does not.

# --------------------------------------------------------------------------- #
# The artwork
# --------------------------------------------------------------------------- #
#: Safe zone each side, as a fraction of the viewBox. The artwork occupies the
#: remaining 76%, and the shapes below are in that interior coordinate space.
SAFE_ZONE = 0.12

#: (x, y, w, h) in the 76x76 interior box.
RECTS: list[tuple[int, int, int, int]] = [
    (0, 0, 30, 24),
    (0, 30, 30, 10),
    (0, 46, 30, 30),
    (36, 0, 40, 16),
    (36, 22, 20, 14),
]

#: The wedge: M36 42 h20 l20 34 h-30 Z
WEDGE: list[tuple[int, int]] = [(36, 42), (56, 42), (76, 76), (46, 76)]

#: The interior box the shapes are drawn in.
INTERIOR = 76

#: Supersampling factor. 8x is where the wedge's diagonal stops showing steps at
#: 16px, which is the size that matters and the size that shows them first.
SUPERSAMPLE = 8

#: One ink for every raster icon, and a mid grey rather than the near-black the
#: dashboard draws the mark in.
#:
#: A raster icon cannot follow a theme. The same bytes land on a light tab strip
#: and a dark one, on white Explorer and on #202020 Explorer, so the ink has to
#: clear the 3:1 non-text contrast floor against all of them at once — which
#: near-black does not: measured, #1a1a1e is 16.62:1 on the light surface and
#: **1.15:1** on the dark one, i.e. invisible, which is exactly how it looked.
#:
#: This is the balance point. Measured against the two theme surfaces and
#: Explorer's dark grey: 3.89:1, 4.93:1, 4.01:1. Nothing darker clears 3:1 on
#: dark, nothing lighter clears it on white; the worst case is best here.
#:
#: The SVG favicon does *not* compromise like this — it carries a
#: prefers-color-scheme rule and gets full-contrast ink in each theme. This
#: value is only for the formats that have no such mechanism.
INK = (126, 126, 126, 255)

#: What gets written where.
PNG_SIZES = (16, 32, 48, 128, 256)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int):
    """The mark at `size` px, transparent background, anti-aliased."""
    from PIL import Image, ImageDraw

    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Interior coordinates -> device pixels, including the safe zone offset.
    offset = SAFE_ZONE * big
    scale = (1 - 2 * SAFE_ZONE) * big / INTERIOR

    def at(x: float, y: float) -> tuple[float, float]:
        return (offset + x * scale, offset + y * scale)

    for x, y, w, h in RECTS:
        x0, y0 = at(x, y)
        x1, y1 = at(x + w, y + h)
        # -1 because PIL's rectangle is inclusive of the far edge, which would
        # otherwise make every shape one supersampled pixel too wide.
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=INK)

    draw.polygon([at(x, y) for x, y in WEDGE], fill=INK)

    return image.resize((size, size), Image.LANCZOS)


def write_outputs(public: Path, packaging: Path) -> list[Path]:
    written: list[Path] = []

    icons_dir = public / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    for size in PNG_SIZES:
        path = icons_dir / f"icon-{size}.png"
        render(size).save(path, "PNG", optimize=True)
        written.append(path)

    # A single .ico carrying every size Windows picks between: the tray and the
    # small shortcut take 16, Explorer's list view 32, its tile view 48, and the
    # installer's own header scales from 128 or 256 depending on DPI. Shipping
    # only the large ones makes Windows downscale, which on this artwork closes
    # the gaps between the blocks.
    frames = [render(size) for size in ICO_SIZES]
    ico = public / "favicon.ico"
    frames[-1].save(ico, "ICO", sizes=[(s, s) for s in ICO_SIZES])
    written.append(ico)

    packaging.mkdir(parents=True, exist_ok=True)
    installer_ico = packaging / "kurukuru.ico"
    installer_ico.write_bytes(ico.read_bytes())
    written.append(installer_ico)

    return written


def _frames(image):
    """Every frame in the file — an .ico holds several, a .png holds one."""
    ico = getattr(image, "ico", None)
    sizes = sorted(ico.sizes()) if ico is not None else None
    if not sizes:
        yield image
        return
    for size in sizes:
        yield ico.getimage(size)


def digest(paths: list[Path]) -> str:
    """A fingerprint of what the icons *look like*, not of their bytes.

    Byte comparison was the first version, and it is wrong across machines: PNG
    and ICO are compressed, and zlib's output differs between platforms — so the
    committed files regenerate to different bytes on Linux than on Windows while
    depicting exactly the same thing. CI found that on its first run, reporting
    the icons stale on a checkout that had not touched them.

    Decoding and hashing the pixels makes the check say what it means: same
    picture, same fingerprint, wherever it was rendered.
    """
    from PIL import Image

    h = hashlib.sha256()
    for path in sorted(paths):
        with Image.open(path) as image:
            for frame in _frames(image):
                h.update(str(frame.size).encode())
                h.update(frame.convert("RGBA").tobytes())
    return h.hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if regenerating would change anything, rather than writing.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    public = root / "frontend" / "public"
    packaging = root / "packaging" / "windows"

    if args.check:
        existing = [
            *(public / "icons").glob("icon-*.png"),
            public / "favicon.ico",
            packaging / "kurukuru.ico",
        ]
        missing = [p for p in existing if not p.exists()]
        if missing or not existing:
            print(f"Icons missing: {[str(p) for p in missing] or 'no icons at all'}")
            return 1
        before = digest(existing)
        written = write_outputs(public, packaging)
        after = digest(written)
        if before != after:
            print(f"Icons are stale: {before} -> {after}. Run tools/make_icons.py.")
            return 1
        print(f"Icons current ({after}).")
        return 0

    written = write_outputs(public, packaging)
    for path in written:
        print(f"  {path.relative_to(root)}  {path.stat().st_size:>7,} bytes")
    print(f"\n{len(written)} files, digest {digest(written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
