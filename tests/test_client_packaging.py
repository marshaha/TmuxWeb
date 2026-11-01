"""静态检查：客户端打包相关的壳文件（CI 工作流、macOS 权限、Web 清单）。

这三个地方出错不会让任何单测变红，但会让发出去的包不可用：
CI 工作流里 `if:` 引用了 secrets 时 GitHub 直接判整份 workflow 无效，
macOS 沙箱缺 network.client 时 app 连一次网络都起不来，
清单留着脚手架默认值时 PWA 装出来叫 tmuxweb_client、主题色是 Flutter 蓝。

YAML 只用正则读那几行，不给 requirements.txt 添一个 PyYAML。
"""

import json
import re
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CLIENT = BASE_DIR / "client"
WORKFLOW = BASE_DIR / ".github" / "workflows" / "mobile.yml"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.lines = self.text.splitlines()

    def test_the_workflow_never_puts_secrets_in_an_if_condition(self):
        # GitHub 的上下文可用性表里 steps.if 不含 secrets；写了就是
        # "Unrecognized named-value: 'secrets'"，整份 workflow 连 job 都不会跑。
        offenders = [line.strip() for line in self.lines if line.strip().startswith("if:") and "secrets." in line]
        self.assertEqual(offenders, [])

    def test_every_client_target_is_built(self):
        # 只看 jobs: 下面那一层（`on:` 里的 push/pull_request 也是两格缩进）。
        block = self.text.split("\njobs:\n", 1)[1]
        jobs = re.findall(r"^  ([a-z][a-z0-9_-]*):\s*$", block, flags=re.MULTILINE)
        self.assertEqual(sorted(jobs), ["android", "ios", "macos", "test", "windows"])

    def test_the_flutter_version_is_pinned(self):
        match = re.search(r'FLUTTER_VERSION:\s*"([^"]+)"', self.text)
        assert match is not None, "FLUTTER_VERSION is not pinned"
        self.assertRegex(match.group(1), r"^\d+\.\d+\.\d+$")

    def test_android_signing_material_is_written_outside_the_checkout(self):
        # 密钥库留在 client/android 里，将来任何一步打包整个目录都会把私钥带出去。
        self.assertIn("runner.temp", self.text)
        self.assertIn("Remove signing material", self.text)
        self.assertNotIn("storeFile=tmuxweb-release.jks", self.text)


class MacosPackagingTests(unittest.TestCase):
    def test_both_entitlements_allow_outbound_network(self):
        for name in ("Release.entitlements", "DebugProfile.entitlements"):
            text = (CLIENT / "macos" / "Runner" / name).read_text()
            self.assertIn("com.apple.security.network.client", text, name)
            self.assertIn("com.apple.security.app-sandbox", text, name)


class WebManifestTests(unittest.TestCase):
    def test_the_manifest_carries_the_product_name_and_brand_colour(self):
        manifest = json.loads((CLIENT / "web" / "manifest.json").read_text())
        self.assertEqual(manifest["name"], "TmuxWeb")
        self.assertEqual(manifest["short_name"], "TmuxWeb")
        self.assertEqual(manifest["theme_color"], "#c96442")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(len(manifest["icons"]), 4)

    def test_the_page_links_the_manifest_and_an_apple_touch_icon(self):
        page = (CLIENT / "web" / "index.html").read_text()
        self.assertIn('rel="manifest"', page)
        self.assertIn('rel="apple-touch-icon"', page)
        self.assertIn("mobile-web-app-capable", page)
        self.assertIn('name="theme-color"', page)
        self.assertNotIn("tmuxweb_client", page)


if __name__ == "__main__":
    unittest.main()
