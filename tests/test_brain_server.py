# -*- coding: utf-8 -*-
"""Gateway 的一轮完整流程：prompt → 模型（假）调 wx_reply → 校验 → 返回。不起真 dsh。"""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from brain.gateway.config import DEFAULTS
from brain.gateway.dsh_client import TurnResult
from brain.gateway.server import Gateway


class FakeDsh:
    """prompt 时按脚本回调网关的 tool_call，模拟模型调工具。"""
    def __init__(self, gw_ref):
        self.gw_ref = gw_ref
        self.script = []          # list of callables(gw) -> None
        self.prompts = []
        self._alive = True

    def start(self): self._alive = True
    def initialize(self, cwd, provider, model): return {}
    def alive(self): return self._alive
    def stop(self): self._alive = False

    def prompt(self, session_id, text, timeout_sec):
        self.prompts.append((session_id, text))
        if self.script:
            self.script.pop(0)(self.gw_ref[0])
        return TurnResult(reasoning="想了一下", text="草稿：不会被发出去", events=[{"type": "turn/end"}])


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.ws = os.path.join(self.data, "workspace")
        os.makedirs(os.path.join(self.ws, "knowledge")); os.makedirs(os.path.join(self.ws, "memory", "people"))
        open(os.path.join(self.ws, "knowledge", "shared.md"), "w").write("# 共享知识\n")
        os.makedirs(os.path.join(self.ws, "skills"))
        json.dump({"ncc-community": {"scope": "all"}}, open(os.path.join(self.ws, "skills", "index.json"), "w"))
        ref = []
        self.fake = FakeDsh(ref)
        self.gw = Gateway(DEFAULTS, self.data, self.ws, dsh_factory=lambda: self.fake)
        ref.append(self.gw)

    def test_reply_roundtrip(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["大理还开着，黄山也在。"]})]
        out = self.gw.handle_reply({"conversation": "肥肉测试1🐶", "is_group": True, "sender": "松爸", "text": "大理还开吗"})
        self.assertEqual(out["bubbles"], ["大理还开着，黄山也在。"])
        self.assertEqual(self.fake.prompts[0][0], "肥肉测试1🐶")
        self.assertIn("[群聊:肥肉测试1🐶 | 发言人:松爸", self.fake.prompts[0][1])
        log = open(os.path.join(self.data, "log", "replies-" + __import__("time").strftime("%Y%m%d") + ".jsonl"), encoding="utf-8").read()
        self.assertIn("大理还开着", log)

    def test_no_reply(self):
        self.fake.script = [lambda gw: gw.tool_call("no_reply", {"reason": "附和"})]
        out = self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "哈哈"})
        self.assertTrue(out["no_reply"]); self.assertEqual(out["reason"], "附和")

    def test_rejected_then_accepted(self):
        def first(gw):
            r = gw.tool_call("wx_reply", {"bubbles": ["一" * 100]})
            assert not r["ok"] and "预算" in r["text"]
            r2 = gw.tool_call("wx_reply", {"bubbles": ["短的。"]})
            assert r2["ok"]
        self.fake.script = [first]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out["bubbles"], ["短的。"])

    def test_silent_model_gets_nudge_then_no_reply(self):
        self.fake.script = [lambda gw: None, lambda gw: None]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertTrue(out["no_reply"]); self.assertEqual(out["reason"], "model_silent")
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn("wx_reply", self.fake.prompts[1][1])

    def test_prime_only_first_time(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["在"]}),
                            lambda gw: gw.tool_call("wx_reply", {"bubbles": ["还在"]})]
        prime = [{"time": "t", "attr": "friend", "sender": "K", "content": "早", "type": "text"}]
        self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "在吗", "prime": prime})
        self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "还在吗", "prime": prime})
        self.assertIn("此前的聊天记录", self.fake.prompts[0][1])
        self.assertNotIn("此前的聊天记录", self.fake.prompts[1][1])

    def test_empty_text_is_no_reply_without_touching_dsh(self):
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "  "})
        self.assertTrue(out["no_reply"]); self.assertEqual(out["reason"], "empty")
        self.assertEqual(self.fake.prompts, [])

    def test_openai_compat_roundtrip(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["西湖边那家", "松爸推荐的"]})]
        out = self.gw.handle_completion({"model": "feirou:group:共建杭州美食地图", "messages": [
            {"role": "system", "content": "人设（会被忽略）"},
            {"role": "user", "content": "小A: 上次那家咖啡店叫啥"},
            {"role": "assistant", "content": "记不清了"},
            {"role": "user", "content": "松爸: 西湖附近有啥推荐"}]})
        self.assertEqual(out["choices"][0]["message"]["content"], "西湖边那家||SPLIT||松爸推荐的")
        self.assertEqual(self.fake.prompts[0][0], "共建杭州美食地图")
        self.assertIn("发言人:松爸", self.fake.prompts[0][1])
        self.assertIn("此前的聊天记录", self.fake.prompts[0][1])
        self.assertIn("小A: 上次那家咖啡店叫啥", self.fake.prompts[0][1])

    def test_tool_call_outside_request(self):
        r = self.gw.tool_call("wx_reply", {"bubbles": ["x"]})
        self.assertFalse(r["ok"])

    def test_propose_creates_pending(self):
        self.fake.script = [lambda gw: (gw.tool_call("propose_shared_knowledge", {"text": "大理涨价", "source": "K说"}),
                                        gw.tool_call("no_reply", {"reason": "记下了"}))]
        self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "大理涨价了"})
        self.assertEqual(self.gw.proposals.list("pending")[0]["text"], "大理涨价")

    def test_dsh_crash_restarts(self):
        self.fake._alive = False
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["ok"]})]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out["bubbles"], ["ok"])
        self.assertTrue(self.fake.alive())


if __name__ == "__main__":
    unittest.main()
