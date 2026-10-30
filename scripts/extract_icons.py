#!/usr/bin/env python3
"""Cut the copy / share glyphs out of icon.png as tintable alpha masks.

The sprite in the pages is inline SVG, but these two glyphs come from the user's own raster icon
sheet; slicing them keeps the shapes exactly as drawn. Each output is a black RGBA image whose alpha
carries the ink, so CSS can tint it with ``background: currentColor`` (see .ico-mask).

Run after icon.png changes:  ./.venv/bin/python scripts/extract_icons.py
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "icon.png"
TARGET = ROOT / "web" / "assets"

# measured from the alpha/luminance runs in icon.png
GLYPHS = {
    "ic-copy.png": (26, 26, 54, 54),
    "ic-share.png": (93, 26, 123, 52),
}


def ink_alpha(tile: Image.Image) -> Image.Image:
    """White becomes transparent, the stroke becomes opaque, antialiasing stays as a ramp."""
    grey = tile.convert("L")
    width, height = grey.size
    mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    source = grey.load()
    target = mask.load()
    for y in range(height):
        for x in range(width):
            value = source[x, y]
            alpha = max(0, min(255, int((255 - value) * 2.0)))
            target[x, y] = (0, 0, 0, alpha)
    return mask


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)
    sheet = Image.open(SOURCE)
    for name, box in GLYPHS.items():
        tile = sheet.crop(box)
        mask = ink_alpha(tile)
        mask.save(TARGET / name)
        opaque = sum(1 for pixel in mask.getdata() if pixel[3] > 40)
        print(f"{name}: {mask.size[0]}x{mask.size[1]}, {opaque} ink pixels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
