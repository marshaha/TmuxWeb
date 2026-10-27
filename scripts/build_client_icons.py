#!/usr/bin/env python3
"""Render the mobile launcher icons from the same SVGs the web app uses.

Run after ``flutter create`` has produced the platform folders:

    .venv/bin/python scripts/build_client_icons.py

Android uses the maskable SVG (full bleed, so the launcher can mask it) and iOS
uses the rounded-tile SVG with opaque corners, which App Store validation
requires (no alpha channel).
"""

from pathlib import Path

import cairosvg

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CLIENT = ROOT / "client"

# density -> pixel size of the legacy square launcher icon
ANDROID = {"mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96, "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192}

# iOS asset name -> pixel size (names come from flutter create's Contents.json)
IOS = {
    "Icon-App-20x20@1x.png": 20, "Icon-App-20x20@2x.png": 40, "Icon-App-20x20@3x.png": 60,
    "Icon-App-29x29@1x.png": 29, "Icon-App-29x29@2x.png": 58, "Icon-App-29x29@3x.png": 87,
    "Icon-App-40x40@1x.png": 40, "Icon-App-40x40@2x.png": 80, "Icon-App-40x40@3x.png": 120,
    "Icon-App-60x60@2x.png": 120, "Icon-App-60x60@3x.png": 180,
    "Icon-App-76x76@1x.png": 76, "Icon-App-76x76@2x.png": 152,
    "Icon-App-83.5x83.5@2x.png": 167,
    "Icon-App-1024x1024@1x.png": 1024,
}


def render(svg: Path, target: Path, size: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".png" and "ios" in target.parts:
        # iOS rejects icons with an alpha channel
        cairosvg.svg2png(url=str(svg), write_to=str(target), output_width=size, output_height=size,
                         background_color="#181713")
    else:
        cairosvg.svg2png(url=str(svg), write_to=str(target), output_width=size, output_height=size)


def main() -> None:
    maskable = WEB / "app-icon-maskable.svg"
    rounded = WEB / "app-icon.svg"
    for svg in (maskable, rounded):
        if not svg.exists():
            raise SystemExit(f"missing source icon: {svg}")

    written = []
    for folder, size in ANDROID.items():
        target = CLIENT / "android" / "app" / "src" / "main" / "res" / folder / "ic_launcher.png"
        render(maskable, target, size)
        written.append((target, size))

    ios_dir = CLIENT / "ios" / "Runner" / "Assets.xcassets" / "AppIcon.appiconset"
    for name, size in IOS.items():
        target = ios_dir / name
        render(rounded, target, size)
        written.append((target, size))

    for target, size in written:
        print(f"{target.relative_to(ROOT)}: {size}x{size}, {target.stat().st_size} bytes")


if __name__ == "__main__":
    main()
