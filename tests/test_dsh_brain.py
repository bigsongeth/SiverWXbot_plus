# -*- coding: utf-8 -*-
"""dsh_brain 插件单测：路由判定 / BrainAPI 与网关的往返（本地假网关，不连微信不连大脑）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from plugins import dsh_brain
from plugins.dsh_brain import store
from plugins.dsh_brain.client import API_ERROR_TEXT, NO_REPLY_TOKEN, SPLIT_SEPARATOR, BrainAPI, split_sender


class _FakeGateway(BaseHTTPRequestHandler):
    """按 text 决定回什么：含 NOREPLY → no_reply；含 ERR → 200+{"error"}；含 BUSY → 503；含 BAD → 非 JSON；否则两条气泡。"""
    seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        _FakeGateway.seen.append((self.path, body))
        text = body.get("text", "")
        if "BUSY" in text:
            out, code = {"error": "busy"}, 503
        elif "ERR" in text:
            out, code = {"error": "dsh init failed"}, 200
        elif "NOREPLY" in text:
            out, code = {"no_reply": True, "reason": "附和"}, 200
        elif "BAD" in text:
            self.send_response(200); self.send_header("Content-Length", "3"); self.end_headers(); self.wfile.write(b"xxx"); return
        else:
            out, code = {"bubbles": ["第一条", "第二条"]}, 200
        raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class BrainApiTest(unittest.TestCase):
    def setUp(self):
        _FakeGateway.seen = []
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeGateway)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_group_bubbles_joined_with_split_and_sender_parsed(self):
        api = BrainAPI("肥肉测试1🐶", True, self.url, 5)
        history = [{"time": "1", "type": "text", "attr": "friend", "sender": "K", "content": "上一句"}]
        out = api.chat("松爸: 黑多岛还能去吗", prompt="人设被忽略", history=history)
        self.assertEqual(out, "第一条" + SPLIT_SEPARATOR + "第二条")
        path, body = _FakeGateway.seen[-1]
        self.assertEqual(path, "/reply")
        self.assertEqual(body["conversation"], "肥肉测试1🐶")
        self.assertTrue(body["is_group"])
        self.assertEqual(body["sender"], "松爸")
        self.assertEqual(body["text"], "黑多岛还能去吗")
        self.assertEqual(body["prime"], history)

    def test_private_sender_is_who(self):
        api = BrainAPI("松爸", False, self.url, 5)
        api.chat("在吗")
        body = _FakeGateway.seen[-1][1]
        self.assertFalse(body["is_group"])
        self.assertEqual(body["sender"], "松爸")
        self.assertEqual(body["text"], "在吗")

    def test_no_reply_token(self):
        self.assertEqual(BrainAPI("g", True, self.url, 5).chat("a: NOREPLY 哈哈"), NO_REPLY_TOKEN)

    def test_error_paths_return_fallback_text(self):
        api = BrainAPI("g", True, self.url, 5)
        self.assertEqual(api.chat("a: ERR"), API_ERROR_TEXT)     # 200 + {"error"}
        self.assertEqual(api.chat("a: BUSY"), API_ERROR_TEXT)    # 503
        self.assertEqual(api.chat("a: BAD"), API_ERROR_TEXT)     # 非 JSON
        dead = BrainAPI("g", True, "http://127.0.0.1:9", 1)
        self.assertEqual(dead.chat("a: 连不上"), API_ERROR_TEXT)  # 连接失败

    def test_at_nickname_residue_stripped(self):
        BrainAPI("g", True, self.url, 5).chat("松爸: （少艾特我）\u2005网络上有啥妙用吗")
        self.assertEqual(_FakeGateway.seen[-1][1]["text"], "网络上有啥妙用吗")
        BrainAPI("g", True, self.url, 5).chat("松爸: （这是正常括号）不带那个空格")
        self.assertEqual(_FakeGateway.seen[-1][1]["text"], "（这是正常括号）不带那个空格")

    def test_heartbeat_ticks_while_waiting(self):
        import sys, types
        from plugins.dsh_brain import client as client_mod
        beats = []
        fake_wd = types.ModuleType("plugins.ui_watchdog"); fake_wd.heartbeat = lambda: beats.append(1)
        old = sys.modules.get("plugins.ui_watchdog"); sys.modules["plugins.ui_watchdog"] = fake_wd
        old_every = client_mod.HEARTBEAT_EVERY_SEC; client_mod.HEARTBEAT_EVERY_SEC = 0.05
        try:
            stop = client_mod._start_heartbeat_ticker()
            import time; time.sleep(0.3); stop.set(); time.sleep(0.1)
            n = len(beats); time.sleep(0.2)
            self.assertGreaterEqual(n, 3)
            self.assertEqual(len(beats), n)   # set() 之后不再跳
        finally:
            client_mod.HEARTBEAT_EVERY_SEC = old_every
            if old is None: sys.modules.pop("plugins.ui_watchdog", None)
            else: sys.modules["plugins.ui_watchdog"] = old

    def test_image_marker_appended(self):
        BrainAPI("g", True, self.url, 5).chat("a: 看这个", image_path="/tmp/x.png")
        self.assertEqual(_FakeGateway.seen[-1][1]["text"], "看这个 [图片]")

    def test_split_sender(self):
        self.assertEqual(split_sender("松爸: 你好"), ("松爸", "你好"))
        self.assertEqual(split_sender("松爸：你好"), ("松爸", "你好"))
        self.assertEqual(split_sender("没有前缀"), ("", "没有前缀"))
        self.assertEqual(split_sender("http://x.y/z"), ("http", "//x.y/z"))  # 已知局限：链接开头会被当前缀，群里实际总带昵称


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dshbrain_")
        self._d, self._c = store.DATA_DIR, store.CONFIG_PATH
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None
        store._cache_mtime = None
        dsh_brain._api_cache.clear()

    def tearDown(self):
        store.DATA_DIR, store.CONFIG_PATH = self._d, self._c
        store._cache = None
        store._cache_mtime = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_all_off_and_no_file_created(self):
        self.assertIsNone(dsh_brain.brain_api_for("肥肉测试1🐶", True))
        self.assertFalse(os.path.exists(store.CONFIG_PATH))

    def test_enabled_group_wildcard_and_exclusion(self):
        store.save({"enabled": True, "gateway_url": "http://127.0.0.1:1", "timeout_sec": 7,
                    "enabled_groups": ["肥肉测试1🐶"], "enabled_chats": ["*"], "excluded_chats": ["文件传输助手"]})
        g = dsh_brain.brain_api_for("肥肉测试1🐶", True)
        self.assertIsInstance(g, BrainAPI)
        self.assertEqual((g.conversation, g.is_group, g.timeout_sec), ("肥肉测试1🐶", True, 7.0))
        self.assertIs(dsh_brain.brain_api_for(" 肥肉测试1🐶 ", True), g)   # 同会话缓存
        self.assertIsNone(dsh_brain.brain_api_for("别的群", True))
        self.assertIsInstance(dsh_brain.brain_api_for("随便谁", False), BrainAPI)
        self.assertIsNone(dsh_brain.brain_api_for("文件传输助手", False))

    def test_master_switch_off_overrides_lists(self):
        store.save({"enabled": False, "enabled_groups": ["*"], "enabled_chats": ["*"]})
        self.assertIsNone(dsh_brain.brain_api_for("x", True))
        self.assertIsNone(dsh_brain.brain_api_for("x", False))

    def test_config_hot_reload(self):
        store.save({"enabled": True, "enabled_groups": ["A"]})
        self.assertIsNotNone(dsh_brain.brain_api_for("A", True))
        cfg = dict(store.load()); cfg["enabled_groups"] = []
        store._cache = None  # 模拟另一个进程写盘：mtime 变了就重读
        with open(store.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        os.utime(store.CONFIG_PATH, None)
        self.assertIsNone(dsh_brain.brain_api_for("A", True))


if __name__ == "__main__":
    unittest.main()
