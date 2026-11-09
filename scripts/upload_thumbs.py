#!/usr/bin/env python3
"""给已经上传过的图片补一份 320px 的 sidecar 缩略图。

聊天气泡里的预览走 `?thumb=1`，服务端优先读 `<upload_files>/.thumbs/<stem>.jpg`。
新上传的图片由浏览器本地生成并顺手存一份；这个脚本只用于**补历史文件**。

    python3 scripts/upload_thumbs.py <目录...>          # 默认 ./upload_files
    python3 scripts/upload_thumbs.py --dry-run <目录>   # 只说要做什么

用 ffmpeg 缩放，不引入 Pillow。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif")
EDGE = 320


def thumb_for(path: str) -> str:
    shots = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(shots, ".thumbs", stem + ".jpg")


def convert(source: str, target: str) -> bool:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    command = ["ffmpeg", "-y", "-loglevel", "error", "-i", source,
               "-vf", f"scale='min({EDGE},iw)':-2", "-frames:v", "1", "-q:v", "4", target]
    completed = subprocess.run(command, capture_output=True)
    if completed.returncode != 0:
        print(f"  ! {os.path.basename(source)}: {completed.stderr.decode(errors='replace').strip()[:120]}")
        return False
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folders", nargs="*", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("需要 ffmpeg 才能缩放图片")
        return 2
    folders = args.folders or ["upload_files"]
    made = skipped = failed = 0
    for folder in folders:
        if not os.path.isdir(folder):
            print(f"跳过（不是目录）：{folder}")
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(IMAGE_EXT) or name.startswith("."):
                continue
            source = os.path.join(folder, name)
            target = thumb_for(source)
            if os.path.isfile(target):
                skipped += 1
                continue
            if args.dry_run:
                print(f"会生成 {target}")
                made += 1
                continue
            if convert(source, target):
                made += 1
                print(f"✓ {name} → {os.path.relpath(target, folder)}")
            else:
                failed += 1
    print(f"生成 {made} · 已存在 {skipped} · 失败 {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
