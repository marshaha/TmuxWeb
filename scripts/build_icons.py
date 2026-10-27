#!/usr/bin/env python3
"""Regenerate the PWA raster icons from the hand-authored SVGs.

The SVGs in ``web/`` are the source of truth: the tab icon is served as vector,
these PNGs only exist for home-screen / PWA installs. Rasterising needs
``cairosvg``, which is a build-time dependency, not a runtime one:

    .venv/bin/pip install cairosvg
    .venv/bin/python scripts/build_icons.py

Icons are ``any`` (rounded tile) and ``maskable`` (full bleed, art inside the
80% safe zone) so Android does not crop the pane layout when it masks the icon.
"""

from pathlib import Path

import cairosvg

WEB = Path(__file__).resolve().parent.parent / "web"
TARGETS = {
    "app-icon.svg": [("app-icon-180.png", 180), ("app-icon-192.png", 192), ("app-icon-512.png", 512),
                     ("app-icon-32.png", 32)],
    "app-icon-maskable.svg": [("app-icon-maskable-192.png", 192), ("app-icon-maskable-512.png", 512)],
}


def main() -> None:
    for source, targets in TARGETS.items():
        svg = WEB / source
        if not svg.exists():
            raise SystemExit(f"missing source icon: {svg}")
        for name, size in targets:
            cairosvg.svg2png(url=str(svg), write_to=str(WEB / name),
                             output_width=size, output_height=size)
            print(f"{name}: {size}x{size}, {(WEB / name).stat().st_size} bytes")


if __name__ == "__main__":
    main()
