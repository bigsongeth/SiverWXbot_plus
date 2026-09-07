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
        if "FLAKY" in text and body.get("attempt", 1) < 2:
            out, code = {"error": "timeout", "reason": "timeout"}, 200   # 第一轮超时、第二轮成功
        elif "BUSY" in text:
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

    def test_retry_new_turn_after_failure(self):
        """第一轮超时 → 隔一下再起一轮，第二轮成功；payload 带 attempt 让网关知道这是重试。"""
        api = BrainAPI("肥肉测试1🐶", True, self.url, timeout_sec=5, max_attempts=2, retry_delay_sec=0)
        self.assertEqual(api.chat("松爸: FLAKY 收一下"), "第一条" + SPLIT_SEPARATOR + "第二条")
        attempts = [b.get("attempt") for _, b in _FakeGateway.seen]
        self.assertEqual(attempts, [1, 2])

    def test_exhausted_returns_fixed_reply_not_fallback(self):
        """2026-09-07 用户拍板：重试耗尽回固定话，不返回失败串（那会触发 model_fallback 切 DeepSeek）。"""
        api = BrainAPI("肥肉测试1🐶", True, self.url, timeout_sec=5, max_attempts=2, retry_delay_sec=0,
                       exhausted_reply="🐶 卡住了")
        self.assertEqual(api.chat("松爸: ERR"), "🐶 卡住了")
        self.assertEqual(len(_FakeGateway.seen), 2)
        dead = BrainAPI("x", False, "http://127.0.0.1:1", timeout_sec=1, max_attempts=2, retry_delay_sec=0,
                        exhausted_reply="🐶 卡住了")
        self.assertEqual(dead.chat("连不上"), "🐶 卡住了")   # 网关整个不可达也照样不切备用

    def test_exhausted_reply_empty_keeps_fallback(self):
        api = BrainAPI("肥肉测试1🐶", True, self.url, timeout_sec=5, max_attempts=2, retry_delay_sec=0, exhausted_reply="")
        self.assertEqual(api.chat("松爸: ERR"), API_ERROR_TEXT)
        self.assertEqual(len(_FakeGateway.seen), 2)

    def test_max_attempts_one_means_no_retry(self):
        api = BrainAPI("肥肉测试1🐶", True, self.url, timeout_sec=5, max_attempts=1, exhausted_reply="🐶 卡住了")
        self.assertEqual(api.chat("松爸: FLAKY"), "🐶 卡住了")
        self.assertEqual(len(_FakeGateway.seen), 1)

    def test_no_reply_counts_as_success_no_retry(self):
        api = BrainAPI("肥肉测试1🐶", True, self.url, timeout_sec=5, max_attempts=3, retry_delay_sec=0)
        self.assertEqual(api.chat("松爸: NOREPLY"), NO_REPLY_TOKEN)
        self.assertEqual(len(_FakeGateway.seen), 1)

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
        self.assertEqual((g.max_attempts, g.retry_delay_sec), (2, 3.0))      # DEFAULT_CONFIG 的重试项透传
        self.assertTrue(g.exhausted_reply)                                   # 默认回固定话，不切备用
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
