# -*- coding: utf-8 -*-
"""回放模拟的单测：数据源转换、规格解析、run() 的统计与对照表（用本地假网关，不碰 dsh）。"""
from __future__ import annotations
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from brain.sim import sources, replay


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class SourcesTest(unittest.TestCase):
    def test_from_qa_jsonl(self):
        p = os.path.join(tempfile.mkdtemp(), "qa.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-08-03T18:30:00", "query": "松爸: 滴滴", "answer": "我刚从键盘上趴起来"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"ts": "2026-08-03T18:31:00", "query": "签到", "answer": "码"}, ensure_ascii=False) + "\n")
        t = sources.from_qa_jsonl(p)
        self.assertEqual(len(t), 1)  # 签到那条被过滤
        self.assertEqual(t[0]["sender"], "松爸"); self.assertEqual(t[0]["text"], "滴滴")
        self.assertEqual(t[0]["old_reply"], "我刚从键盘上趴起来"); self.assertFalse(t[0]["is_group"])

    def test_from_qa_jsonl_no_prefix_and_split_marker(self):
        p = os.path.join(tempfile.mkdtemp(), "qa.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"query": "请问带小孩的可以入住吗", "answer": "不敢拍板\n||SPLIT||\n你去大理还是黄山?"}, ensure_ascii=False) + "\n")
            f.write("\n")
        t = sources.from_qa_jsonl(p)
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["sender"], "对方")
        self.assertEqual(t[0]["text"], "请问带小孩的可以入住吗")
        self.assertEqual(t[0]["old_reply"], "不敢拍板\n你去大理还是黄山?")
        self.assertEqual(t[0]["prime"], [])

    def test_from_memory_json_pairs_friend_with_following_self(self):
        p = os.path.join(tempfile.mkdtemp(), "m.json")
        _write_json(p, [
            {"time": "1", "type": "text", "attr": "friend", "sender": "A", "content": "大理还开吗"},
            {"time": "2", "type": "text", "attr": "self", "sender": "肥肉", "content": "开着"},
            {"time": "3", "type": "text", "attr": "self", "sender": "肥肉", "content": "黄山也开"},
            {"time": "4", "type": "text", "attr": "friend", "sender": "B", "content": "哈哈"},
        ])
        t = sources.from_memory_json(p, "测试群", True)
        self.assertEqual(len(t), 2)
        self.assertEqual(t[0]["conversation"], "sim-测试群"); self.assertTrue(t[0]["is_group"])
        self.assertEqual(t[0]["old_reply"], "开着\n黄山也开")
        self.assertEqual(t[1]["old_reply"], "")
        self.assertEqual([x["content"] for x in t[1]["prime"]], ["大理还开吗", "开着", "黄山也开"])

    def test_from_memory_json_keeps_quote_replies_drops_system_and_checkin(self):
        # 真机样本：机器人的引用回复 type="quote"；system/time 条目与签到往来都不进回放
        p = os.path.join(tempfile.mkdtemp(), "m.json")
        _write_json(p, [
            {"time": "0", "type": "time", "attr": "system", "sender": "system", "content": "11:44"},
            {"time": "1", "type": "text", "attr": "friend", "sender": "松爸", "content": "@肥肉 这家黄牛肉馆"},
            {"time": "2", "type": "quote", "attr": "self", "sender": "self", "content": "🐶 贵州人认证可太硬了"},
            {"time": "3", "type": "image", "attr": "friend", "sender": "松爸", "content": "[图片]"},
            {"time": "4", "type": "quote", "attr": "friend", "sender": "K", "content": "引用一下：那家在哪"},
            {"time": "5", "type": "text", "attr": "friend", "sender": "K", "content": "签到"},
            {"time": "6", "type": "text", "attr": "self", "sender": "self", "content": "签到成功 ✅ 兑换码：BTC-44ED-6TGS-GBPM"},
        ])
        t = sources.from_memory_json(p, "肥肉测试1🐶", True)
        self.assertEqual([x["text"] for x in t], ["@肥肉 这家黄牛肉馆", "引用一下：那家在哪"])
        self.assertEqual(t[0]["old_reply"], "🐶 贵州人认证可太硬了")
        self.assertEqual(t[1]["old_reply"], "")
        # prime 里 quote 归一成 text（网关 filter_prime 只认 type=text），system 条目不在其中
        self.assertEqual([(x["attr"], x["type"]) for x in t[1]["prime"]], [("friend", "text"), ("self", "text")])


class SpecTest(unittest.TestCase):
    def test_memory_spec_with_and_without_limit(self):
        self.assertEqual(replay.parse_memory_spec("a/b.json:肥肉测试1🐶:group"), ("a/b.json", "肥肉测试1🐶", True, None))
        self.assertEqual(replay.parse_memory_spec("a/b.json:松爸:private:15"), ("a/b.json", "松爸", False, 15))

    def test_qa_spec_with_and_without_limit(self):
        self.assertEqual(replay.parse_qa_spec("~/sim/qa-20260904.jsonl"), ("~/sim/qa-20260904.jsonl", None))
        self.assertEqual(replay.parse_qa_spec("~/sim/qa-20260904.jsonl:8"), ("~/sim/qa-20260904.jsonl", 8))

    def test_tail_and_replied_only(self):
        turns = [{"old_reply": ""}, {"old_reply": "x"}, {"old_reply": ""}, {"old_reply": "y"}]
        self.assertEqual(replay.select(turns, 2, False), [{"old_reply": ""}, {"old_reply": "y"}])
        self.assertEqual(replay.select(turns, None, True), [{"old_reply": "x"}, {"old_reply": "y"}])
        self.assertEqual(replay.select(turns, 1, True), [{"old_reply": "y"}])


class _FakeGateway(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        _FakeGateway.seen.append(body)
        text = body.get("text", "")
        if text == "沉默":
            out = {"no_reply": True, "reason": "not_addressed"}
        elif text == "长":
            out = {"bubbles": ["好呀 " * 200]}
        elif text == "套话":
            out = {"bubbles": ["好呀朋友", "需要我再讲讲吗？"]}
        else:
            out = {"bubbles": ["好呀朋友，开着"]}
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class RunTest(unittest.TestCase):
    def setUp(self):
        _FakeGateway.seen = []
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeGateway)
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.th.join(2)

    def _turn(self, text, **kw):
        d = {"conversation": "sim-测试群", "is_group": True, "sender": "A", "text": text, "old_reply": "老回复|带竖线", "prime": []}
        d.update(kw)
        return d

    def test_run_writes_table_and_stats(self):
        out = os.path.join(tempfile.mkdtemp(), "r.md")
        turns = [self._turn("大理还开吗"), self._turn("黄山呢"), self._turn("沉默"), self._turn("长"), self._turn("套话")]
        st = replay.run(turns, self.url, out)
        self.assertEqual(st["n"], 5)
        self.assertEqual(st["no_reply"], 1)
        self.assertEqual(st["over_budget"], 1)
        self.assertEqual(st["closing"], 1)
        self.assertGreaterEqual(st["repeat_opener"], 1)  # "好呀朋友" 开头出现了多次
        self.assertIn("avg_ms", st)
        self.assertEqual(len(_FakeGateway.seen), 5)
        self.assertEqual(set(_FakeGateway.seen[0].keys()), {"conversation", "is_group", "sender", "text", "prime"})
        with open(out, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("| # | 会话 | 发言 | 原回复 | 新回复 | 闸门 | 耗时ms |", md)
        self.assertIn("老回复\\|带竖线", md)      # 竖线转义，别把表格撑坏
        self.assertIn("〔不接话：not_addressed〕", md)
        self.assertEqual(md.count("\n| "), 1 + 5)  # 表头 + 5 行（分隔行是 |---|，不算）

    def test_run_survives_gateway_error(self):
        out = os.path.join(tempfile.mkdtemp(), "r.md")
        st = replay.run([self._turn("x")], "http://127.0.0.1:1", out)  # 没人监听
        self.assertEqual(st["n"], 1); self.assertEqual(st["no_reply"], 1)
        with open(out, encoding="utf-8") as f:
            self.assertIn("〔不接话：", f.read())


if __name__ == "__main__":
    unittest.main()
