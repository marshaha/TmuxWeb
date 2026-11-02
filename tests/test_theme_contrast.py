"""Theme guard rails.

用户原话：「主题 claudecode 在窗口模式时，背景和字体都是白色的 看不清（设置里面好像也有类似问题）
所有主题都应该有类似的防呆措施」。

这次事故的根因：页面模板里写死了浅色底（`#fff`、`#eceae3`、`#e9e7df`…），主题只重指向了
`--ink` 之类的文字色，于是深色主题下就成了"浅字 + 白底"。

防呆分三层，都在这个文件里：
1. 每套主题必须定义同一组语义变量（card / card-hover / bubble / line-strong / shimmer）；
2. 页面模板和静态资源不许再写死浅色底，一律用变量（theme.css 自己是调色板，豁免）；
3. 会随主题变色的几个关键页面必须引入 theme.css + theme.js。
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ASSETS = WEB / "assets"

SEMANTIC = ("--card", "--card-hover", "--bubble", "--line-strong", "--shimmer")

# 写死的浅色底：background:#fff / white / rgba(255,255,255,.8) / 米色卡片底
SURFACE_LITERAL = re.compile(
    r"background\s*:\s*(?:#fff(?:fff)?\b|white\b|rgba\(\s*255\s*,\s*255\s*,\s*255)",
    re.IGNORECASE,
)
# var(--x,#fff) 这种兜底是允许的
VAR_FALLBACK = re.compile(r"var\(\s*--[a-z0-9-]+\s*,\s*#fff\b\s*\)", re.IGNORECASE)
# 任何"亮色底"（写死 hex），亮度阈值 0.65：深色主题下它会变成一块发光的方块
HEX_BG = re.compile(r"background\s*:\s*(#[0-9a-fA-F]{3,6})\b")
LIGHT_LUM = 0.65


def _lum(hexs: str):
    h = hexs.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


PAGES = sorted(p.name for p in WEB.glob("*.html"))
STATIC = [p for p in (list(ASSETS.glob("*.js")) + list(ASSETS.glob("*.css"))) if p.name != "theme.css"]


class ThemePaletteTests(unittest.TestCase):
    def test_every_theme_defines_the_semantic_surface_variables(self):
        css = (ASSETS / "theme.css").read_text()
        blocks = re.findall(r':root\[data-theme="([a-z-]+)"\]\{(.*?)\}', css, re.S)
        self.assertGreaterEqual(len(blocks), 5, "主题色板少了")
        for name, body in blocks:
            for var in SEMANTIC:
                self.assertIn(var + ":", body, f"主题 {name} 缺 {var}")
            # 深色主题必须声明 color-scheme，滚动条/表单控件才会跟着暗
            if name in ("claude-code", "night"):
                self.assertIn("color-scheme:dark", body, name)

    def test_dark_themes_keep_the_chat_surfaces_dark(self):
        css = (ASSETS / "theme.css").read_text()
        for name in ("claude-code", "night"):
            themed = re.search(rf':root\[data-theme="{name}"\]\{{(.*?)\}}', css, re.S)
            self.assertIsNotNone(themed, name)
            block = themed.group(1)
            match = re.search(r"--card:(#[0-9a-f]{6})", block)
            self.assertIsNotNone(match, name)
            card = match.group(1)
            r, g, b = (int(card[i:i + 2], 16) for i in (1, 3, 5))
            self.assertLess((r + g + b) / 3, 80, f"{name} 的 --card 太亮：{card}")


class NoHardcodedSurfaceTests(unittest.TestCase):
    def _offenders(self, path: pathlib.Path):
        text = VAR_FALLBACK.sub("var(--x)", path.read_text())
        out = []
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in SURFACE_LITERAL.finditer(line):
                out.append((path.name, lineno, m.group(0), line.strip()[:70]))
            for m in HEX_BG.finditer(line):
                l = _lum(m.group(1))
                if l is not None and l > LIGHT_LUM:
                    out.append((path.name, lineno, m.group(1), line.strip()[:70]))
        return out

    def test_page_templates_do_not_hardcode_light_surfaces(self):
        bad = []
        for name in PAGES:
            bad += self._offenders(WEB / name)
        self.assertEqual(bad, [], "页面里还写着浅色底，深色主题会白底白字：" + repr(bad[:6]))

    def test_shared_assets_do_not_hardcode_light_surfaces(self):
        bad = []
        for p in STATIC:
            bad += self._offenders(p)
        self.assertEqual(bad, [], "共享资源里还写着浅色底：" + repr(bad[:6]))

    def test_the_chat_preview_uses_theme_variables(self):
        """窗口模式里内嵌的就是 /preview：气泡、正文、表格头、输入框都必须吃主题变量。"""
        page = (WEB / "preview.html").read_text()
        self.assertIn(".bubble{", page)
        self.assertIn("background:var(--bubble)", page)
        self.assertIn("background:var(--card)", page)
        self.assertIn(".body th{background:var(--surface2)}", page)
        self.assertIn(".body :not(pre)>code{", page)
        self.assertIn("background:var(--surface2)", page)

    def test_pages_that_can_be_themed_load_the_theme_assets(self):
        for name in PAGES:
            page = (WEB / name).read_text()
            if "/assets/theme.css" in page:
                self.assertIn("/assets/theme.js", page, name)
        # 主要几个页面必须在
        for name in ("index.html", "home.html", "grid.html", "preview.html", "term.html"):
            self.assertIn("/assets/theme.css", (WEB / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
