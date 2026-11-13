"""自测脚本的判定规则：什么才算"走 MagicDNS"。

真机上下载线路靠 `scripts/selfcheck_download.py` 实测（会真的拉字节），
这里只把它的两条判断钉住，免得规则被改松了还不自知。
"""
import unittest

import importlib.util
import sys
from pathlib import Path

# scripts/ 不是包：按路径加载，免得为了一个测试加 __init__.py 干扰运行脚本
_SPEC = importlib.util.spec_from_file_location(
    "selfcheck_download",
    Path(__file__).resolve().parent.parent / "scripts" / "selfcheck_download.py")
check = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("selfcheck_download", check)
_SPEC.loader.exec_module(check)


class MagicDNSNameTests(unittest.TestCase):
    def test_a_tailnet_name_is_magicdns(self):
        self.assertTrue(check.is_magicdns("https://robo.neko-chroma.ts.net"))
        self.assertTrue(check.is_magicdns("https://robo.neko-chroma.ts.net/t/download?t=x"))

    def test_a_bare_tailnet_ip_is_not(self):
        self.assertFalse(check.is_magicdns("https://100.123.123.3:8787"))
        self.assertFalse(check.is_magicdns("http://100.123.123.3:8787/t/download"))

    def test_the_public_domain_is_not(self):
        self.assertFalse(check.is_magicdns("https://tmux.122990.xyz"))
        self.assertFalse(check.is_magicdns("https://ddr.167990.xyz"))


class OriginPickTests(unittest.TestCase):
    def test_the_magicdns_name_wins_over_the_lan_address(self):
        entries = [
            {"url": "https://tmux.122990.xyz", "kind": "public", "transfer": False},
            {"url": "https://robo.neko-chroma.ts.net", "kind": "tailnet", "transfer": True},
        ]
        self.assertEqual(check.pick_origin(entries)["url"], "https://robo.neko-chroma.ts.net")

    def test_without_a_magicdns_name_it_falls_back_to_another_direct_origin(self):
        entries = [{"url": "https://relay.example.com", "kind": "transfer", "transfer": True}]
        self.assertEqual(check.pick_origin(entries)["url"], "https://relay.example.com")

    def test_nothing_usable_means_no_origin(self):
        self.assertIsNone(check.pick_origin([]))


if __name__ == "__main__":
    unittest.main()
