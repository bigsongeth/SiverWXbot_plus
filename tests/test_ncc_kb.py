# -*- coding: utf-8 -*-
"""ncc_kb 插件单测：路由判定 / 配置读写 / prompt 覆盖（不连微信、不连知识库）。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest

from plugins import ncc_kb
from plugins.ncc_kb import store


class FakeBotConfig:
    def __init__(self):
        self.prompts = {"NCC肥肉": "你是肥肉，NCC助手。", "默认": "通用助手。"}

    def get_prompt_content(self, name):
        return self.prompts.get(name, "")


class FakeBot:
    def __init__(self):
        self.config = FakeBotConfig()


class NccKbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncckb_")
        self._d, self._c = store.DATA_DIR, store.CONFIG_PATH
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None
        store._cache_mtime = None
        ncc_kb._api_cache.clear()
        # 注入假的 wxbot_core，让 _build_api 能懒导入到我们的假接口类
        self._orig_mod = sys.modules.get("wxbot_core")
        fake = types.ModuleType("wxbot_core")

        class _Api:
            def __init__(self, cfg):
                self.base_url = getattr(cfg, "base_url", "")
                self.model1 = getattr(cfg, "model1", "")
                self.api_sdk = getattr(cfg, "api_sdk", "")

        fake.DusAPI = _Api
        fake.OpenAIAPI = _Api
        sys.modules["wxbot_core"] = fake
        self.bot = FakeBot()

    def tearDown(self):
        store.DATA_DIR, store.CONFIG_PATH = self._d, self._c
        store._cache = None
        store._cache_mtime = None
        if self._orig_mod is not None:
            sys.modules["wxbot_core"] = self._orig_mod
        else:
            sys.modules.pop("wxbot_core", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_config_created(self):
        cfg = ncc_kb.get_config()
        self.assertIn("肥肉测试1", cfg["enabled_groups"])
        self.assertEqual(cfg["endpoint"]["model"], "ncc-kb")

    def test_group_enabled_returns_api(self):
        api = ncc_kb.kb_api_for(self.bot, "肥肉测试1", True)
        self.assertIsNotNone(api)
        self.assertEqual(api.base_url, "http://100.71.182.5:8434")
        self.assertEqual(api.model1, "ncc-kb")

    def test_group_disabled_returns_none(self):
        self.assertIsNone(ncc_kb.kb_api_for(self.bot, "某个没开的群", True))

    def test_private_toggle(self):
        self.assertIsNone(ncc_kb.kb_api_for(self.bot, "张三", False))
        ncc_kb.toggle("张三", False, True)
        self.assertIsNotNone(ncc_kb.kb_api_for(self.bot, "张三", False))
        ncc_kb.toggle("张三", False, False)
        self.assertIsNone(ncc_kb.kb_api_for(self.bot, "张三", False))

    def test_wildcard_enables_all_private_chats(self):
        """enabled_chats 写 "*" 时任意私聊都算开启（私聊一直在新增，逐个列名字维护不动）。"""
        cfg = ncc_kb.get_config()
        cfg["enabled_chats"] = ["*"]
        ncc_kb.save_config(cfg)
        for who in ("松爸", "从没见过的新朋友", "Zz.酱🐳"):
            self.assertTrue(ncc_kb.kb_enabled(who, False), who)
        # 通配符按会话类型隔离：私聊全开不影响群聊
        self.assertFalse(ncc_kb.kb_enabled("某个没开的群", True))

    def test_excluded_beats_wildcard(self):
        """排除名单优先于通配，这样"全开但某几个不要"不用退回逐个列举。"""
        cfg = ncc_kb.get_config()
        cfg["enabled_chats"] = ["*"]
        cfg["excluded_chats"] = ["文件传输助手"]
        ncc_kb.save_config(cfg)
        self.assertFalse(ncc_kb.kb_enabled("文件传输助手", False))
        self.assertIsNone(ncc_kb.kb_api_for(self.bot, "文件传输助手", False))
        self.assertTrue(ncc_kb.kb_enabled("别人", False))

    def test_excluded_beats_explicit_name(self):
        """显式列名的会话也能被排除名单否掉。"""
        cfg = ncc_kb.get_config()
        cfg["enabled_groups"] = ["肥肉测试1"]
        cfg["excluded_groups"] = ["肥肉测试1"]
        ncc_kb.save_config(cfg)
        self.assertFalse(ncc_kb.kb_enabled("肥肉测试1", True))

    def test_prompt_override_when_enabled(self):
        self.assertEqual(ncc_kb.kb_prompt_for(self.bot, "肥肉测试1", True), "你是肥肉，NCC助手。")
        self.assertIsNone(ncc_kb.kb_prompt_for(self.bot, "没开的群", True))

    def test_prompt_none_when_prompt_name_blank(self):
        cfg = ncc_kb.get_config()
        cfg["prompt_name"] = ""
        ncc_kb.save_config(cfg)
        self.assertIsNone(ncc_kb.kb_prompt_for(self.bot, "肥肉测试1", True))

    def test_set_enabled_dedup_and_trim(self):
        ncc_kb.set_enabled(["群A", " 群A ", "群B", ""], True)
        cfg = ncc_kb.get_config()
        self.assertEqual(cfg["enabled_groups"], ["群A", "群B"])

    def test_api_cache_rebuilds_on_endpoint_change(self):
        a1 = ncc_kb.kb_api_for(self.bot, "肥肉测试1", True)
        cfg = ncc_kb.get_config()
        cfg["endpoint"]["url"] = "http://100.71.182.5:9999"
        ncc_kb.save_config(cfg)
        a2 = ncc_kb.kb_api_for(self.bot, "肥肉测试1", True)
        self.assertEqual(a2.base_url, "http://100.71.182.5:9999")
        self.assertIsNot(a1, a2)

    def test_enable_disable_persists_to_disk(self):
        ncc_kb.toggle("持久群", True, True)
        store._cache = None  # 强制从磁盘重读
        self.assertTrue(ncc_kb.kb_enabled("持久群", True))


if __name__ == "__main__":
    unittest.main()
