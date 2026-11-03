#!/usr/bin/env python3
"""Web ⇄ Flutter parity gate.

The console (web) and the Flutter client (`client/`) both talk to `tmux_web.py`.
When a web feature lands without its Dart twin the two drift apart silently and
nobody notices until the user opens the app. This script makes the drift loud:

  1. every HTTP route the server exposes            (`tmux_web.py`)
  2. every route the Flutter client calls           (`client/lib/api/api_client.dart`)
  3. every shared web module / page                 (`web/assets/*.js`, `web/*.html`)

`parity.json` is the contract. Each entry says either *where* the Dart twin
lives (and that file must exist) or *why* the thing is web-only:

    {
      "ported_api":     {"/api/panes": "client/lib/api/api_client.dart"},
      "web_only_api":   {"/api/panes/{}/files/stat": "reason"},
      "ported_assets":  {"theme.js": "client/lib/theme.dart"},
      "web_only_assets":{"shell.js": "reason"},
      "ported_pages":   {"home.html": "client/lib/screens/home_board_screen.dart"},
      "web_only_pages": {"preview.html": "reason"}
    }

The gate fails on anything unlisted, on a `ported_*` entry whose Dart file is
gone, and on an entry whose web side no longer exists. UI parity itself cannot
be verified from files — the value here is that a new endpoint/module/page
cannot be added without an explicit decision written down.

Usage
    python3 scripts/parity.py            # report, exit 1 when the gate fails
    python3 scripts/parity.py --update   # refresh parity.json from the code
    python3 scripts/parity.py --json     # machine-readable report
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tmux_web.py"
DART_API = ROOT / "client" / "lib" / "api" / "api_client.dart"
WEB = ROOT / "web"
MANIFEST = ROOT / "parity.json"

ROUTE_RE = re.compile(r'@app\.(get|post|put|delete|patch|websocket)\(\s*"([^"]+)"')
DART_RE = re.compile(r"_uri\(\s*'([^']+)'|_uri\(\s*\"([^\"]+)\"")

KINDS = ("api", "assets", "pages")


def normalise(path: str) -> str:
    """`{pane_id}` / `${paneRef}` are the same placeholder on both sides.

    Order matters: replace the whole `${…}` first, otherwise the inner braces
    turn `${paneRef}` into `${}` and the two sides never match.
    """
    cleaned = re.sub(r"\$\{[^}]*\}", "{}", path)
    cleaned = re.sub(r"\{[^}]*\}", "{}", cleaned)
    return cleaned.rstrip("/") or "/"


def server_routes() -> set[str]:
    text = SERVER.read_text()
    return {normalise(path) for _method, path in ROUTE_RE.findall(text)
            if path.startswith("/api")}


def dart_routes() -> set[str]:
    if not DART_API.exists():
        return set()
    text = DART_API.read_text()
    found = set()
    for single, double in DART_RE.findall(text):
        path = single or double
        if path.startswith("/api"):
            found.add(normalise(path))
    return found


def web_assets() -> set[str]:
    return {path.name for path in (WEB / "assets").glob("*.js")}


def web_pages() -> set[str]:
    return {path.name for path in WEB.glob("*.html")}


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        data = json.loads(MANIFEST.read_text())
    except ValueError as exc:
        raise SystemExit(f"parity.json is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("parity.json must be an object")
    return data


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def dart_file_exists(relative: str) -> bool:
    return (ROOT / relative).exists()


def report() -> dict:
    manifest = load_manifest()
    api, assets, pages = server_routes(), web_assets(), web_pages()
    dart = dart_routes()

    def check(kind: str, everything: set[str], covered: set[str]) -> dict:
        ported = manifest.get(f"ported_{kind}", {}) or {}
        web_only = manifest.get(f"web_only_{kind}", {}) or {}
        pending = manifest.get(f"pending_{kind}", {}) or {}
        unlisted = sorted(item for item in everything
                          if item not in covered and item not in ported
                          and item not in web_only and item not in pending)
        stale = sorted(item for item in [*ported, *web_only, *pending]
                       if item not in everything)
        broken = sorted(item for item, target in ported.items()
                        if item in everything and not dart_file_exists(target))
        return {"unlisted": unlisted, "stale": stale, "broken": broken,
                "ported": len(ported), "web_only": len(web_only),
                "pending": len(pending), "total": len(everything),
                "pending_items": sorted(item for item in pending if item in everything),
                "covered": sorted(everything & covered)}

    api_report = check("api", api, dart)
    assets_report = check("assets", assets, set())
    pages_report = check("pages", pages, set())
    problems = sum(len(block[key]) for block in (api_report, assets_report, pages_report)
                   for key in ("unlisted", "stale", "broken"))
    return {"api": api_report, "assets": assets_report, "pages": pages_report,
            "problems": problems, "dart_routes": len(dart), "server_routes": len(api)}


def print_report(data: dict) -> None:
    def block(title: str, entry: dict, noun: str) -> None:
        print(f"{title}: {entry['total']} 项 · 已覆盖 {len(entry['covered'])} · "
              f"Dart {entry['ported']} · web-only {entry['web_only']} · 待移植 {entry['pending']}")
        for item in entry["pending_items"]:
            print(f"    待移植 {item}")
        for label, key, hint in (
            ("未声明", "unlisted", f"→ 补 Dart，或写进 parity.json 的 web_only_{noun}"),
            ("Dart 文件不存在", "broken", "→ 改成真实路径"),
            ("清单里已不存在", "stale", "→ 删掉这条"),
        ):
            if entry[key]:
                print(f"  {label} ({len(entry[key])}) {hint}")
                for item in entry[key]:
                    print(f"    {item}")

    block("API", data["api"], "api")
    block("共享模块", data["assets"], "assets")
    block("页面", data["pages"], "pages")
    print("parity: OK" if data["problems"] == 0 else
          f"parity: {data['problems']} 处需要处理")


def update() -> None:
    """Refresh the manifest, keeping every reason that still applies."""
    manifest = load_manifest()
    api, assets, pages = server_routes(), web_assets(), web_pages()
    dart = dart_routes()
    fresh: dict[str, dict[str, str]] = {}

    def keep(kind: str, everything: set[str]) -> None:
        for entry in ("ported", "web_only", "pending"):
            for item, target in (manifest.get(f"{entry}_{kind}", {}) or {}).items():
                if item in everything:
                    fresh.setdefault(f"{entry}_{kind}", {})[item] = target

    keep("api", api)
    keep("assets", assets)
    keep("pages", pages)

    # 新增的 API 若 Dart 已经在调，就直接标成 ported；否则留成 web-only 待定
    for route in sorted(api - set(fresh.get("ported_api", {}))):
        if route in fresh.get("web_only_api", {}):
            continue
        if route in dart:
            fresh.setdefault("ported_api", {})[route] = "client/lib/api/api_client.dart"
        else:
            fresh.setdefault("web_only_api", {})[route] = "TODO: 还没移植到 Dart"
    save_manifest(fresh)
    print(f"wrote {MANIFEST.relative_to(ROOT)}")


def main(argv: list[str]) -> int:
    if "--update" in argv:
        update()
        return 0
    data = report()
    if "--json" in argv:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_report(data)
    return 0 if data["problems"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
