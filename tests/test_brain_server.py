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
        self.stopped = 0
        self.timeout_next = False       # 为 True 时 prompt 直接返回 timed_out
        self.init_fail_once = False     # 为 True 时 initialize 抛一次
        self.error_next = False         # 为 True 时 prompt 直接返回 error
        self.error_at = None            # 第 N 次 prompt（1 起）返回 error，用来打 nudge 那轮
        self.cancels = []               # cancel 被调过的 session_id
        self.cancel_ok = True           # False 模拟 cancel 等不到 idle

    def start(self): self._alive = True
    def initialize(self, cwd, provider, model):
        if self.init_fail_once:
            self.init_fail_once = False
            raise RuntimeError("initialize: boom")
        return {}
    def alive(self): return self._alive
    def stop(self):
        self._alive = False
        self.stopped += 1
    def cancel(self, session_id):
        self.cancels.append(session_id)
        return self.cancel_ok

    def prompt(self, session_id, text, timeout_sec):
        self.prompts.append((session_id, text))
        if self.script:
            self.script.pop(0)(self.gw_ref[0])
        if self.timeout_next:
            return TurnResult(reasoning="卡住了", timed_out=True)
        if self.error_next or self.error_at == len(self.prompts):
            self.error_next = False
            return TurnResult(reasoning="", error="id collision", events=[{"type": "turn/end"}])
        return TurnResult(reasoning="想了一下", text="草稿：不会被发出去", events=[{"type": "turn/end"}])


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.ws = os.path.join(self.data, "workspace")
        os.makedirs(os.path.join(self.ws, "knowledge")); os.makedirs(os.path.join(self.ws, "memory", "people"))
        with open(os.path.join(self.ws, "knowledge", "shared.md"), "w", encoding="utf-8") as f:
            f.write("# 共享知识\n")
        os.makedirs(os.path.join(self.ws, "skills"))
        with open(os.path.join(self.ws, "skills", "index.json"), "w", encoding="utf-8") as f:
            json.dump({"ncc-community": {"scope": "all"}}, f)
        ref = []
        self.fake = FakeDsh(ref)
        self.gw = Gateway(DEFAULTS, self.data, self.ws, dsh_factory=lambda: self.fake)
        ref.append(self.gw)

    def test_reply_roundtrip(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["大理还开着，黄山也在。"]})]
        out = self.gw.handle_reply({"conversation": "肥肉测试1🐶", "is_group": True, "sender": "松爸", "text": "大理还开吗"})
        self.assertEqual(out["bubbles"], ["大理还开着，黄山也在。"])
        # 会话 ID 现在包含 epoch
        self.assertTrue(self.fake.prompts[0][0].startswith("肥肉测试1🐶#"))
        self.assertIn("[群聊:肥肉测试1🐶 | 发言人:松爸", self.fake.prompts[0][1])
        with open(os.path.join(self.data, "log", "replies-" + __import__("time").strftime("%Y%m%d") + ".jsonl"), encoding="utf-8") as f:
            log = f.read()
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
        # 会话 ID 现在包含 epoch
        self.assertTrue(self.fake.prompts[0][0].startswith("共建杭州美食地图#"))
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

    # ---- 复审修补：轮次标识 / 超时重建 / 生命周期 ----
    def test_stale_turn_callback_rejected(self):
        ids = []

        def first(gw):
            ids.append(gw.inflight.turn_id)
            gw.tool_call("wx_reply", {"bubbles": ["第一轮"], "turn_id": gw.inflight.turn_id})

        def second(gw):
            r = gw.tool_call("wx_reply", {"bubbles": ["串话"], "turn_id": ids[0]})
            assert r["ok"] is False, r
            r2 = gw.tool_call("wx_reply", {"bubbles": ["正确"], "turn_id": gw.inflight.turn_id})
            assert r2["ok"], r2
        self.fake.script = [first, second]
        self.gw.handle_reply({"conversation": "A", "is_group": False, "sender": "A", "text": "在吗"})
        out = self.gw.handle_reply({"conversation": "B", "is_group": False, "sender": "B", "text": "你好"})
        self.assertEqual(out["bubbles"], ["正确"])
        self.assertNotEqual(ids[0], self.fake.prompts[1][1])  # 两轮 id 不同

    def test_prefix_line_carries_turn_id(self):
        ids = []
        self.fake.script = [lambda gw: (ids.append(gw.inflight.turn_id),
                                        gw.tool_call("wx_reply", {"bubbles": ["在"], "turn_id": gw.inflight.turn_id}))]
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "在吗"})
        first_line = self.fake.prompts[0][1].split("\n")[0]
        self.assertIn("| 轮次:" + ids[0] + "]", first_line)
        self.assertTrue(first_line.startswith("[群聊:K | 发言人:K"))

    def test_missing_turn_id_accepted_but_logged(self):
        seen = []
        self.fake.script = [lambda gw: (gw.tool_call("wx_reply", {"bubbles": ["无标识"]}),
                                        seen.extend(gw.inflight.tool_log))]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out["bubbles"], ["无标识"])
        self.assertIn({"tool": "wx_reply", "no_turn_id": True}, seen)

    def test_timeout_cancels_turn_and_keeps_dsh(self):
        self.fake.timeout_next = True
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out, {"no_reply": True, "reason": "timeout"})
        self.assertEqual(len(self.fake.cancels), 1)
        self.assertTrue(self.fake.cancels[0].startswith("K#"))
        self.assertEqual(self.fake.stopped, 0)        # 进程保住
        self.assertIs(self.gw.dsh, self.fake)
        self.assertIn("K", self.gw.primed)            # 预热保住，下一条不重灌历史
        self.assertEqual(len(self.fake.prompts), 1)   # 超时后不再 nudge
        log = self.gw.read_log(1)[0]
        self.assertTrue(log["dsh_cancelled_after_timeout"])
        self.assertFalse(log["dsh_restarted_after_timeout"])

    def test_timeout_restarts_dsh_when_cancel_fails(self):
        self.fake.timeout_next = True
        self.fake.cancel_ok = False
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out, {"no_reply": True, "reason": "timeout"})
        self.assertEqual(self.fake.stopped, 1)
        self.assertIsNone(self.gw.dsh)
        self.assertEqual(len(self.fake.prompts), 1)
        log = self.gw.read_log(1)[0]
        self.assertTrue(log["dsh_restarted_after_timeout"])
        self.assertFalse(log["dsh_cancelled_after_timeout"])

    def test_ensure_dsh_stops_old_client(self):
        ref = [None]
        fakes = []

        def factory():
            f = FakeDsh(ref)
            n = len(fakes)   # 两次文案不同，避免撞上同会话的最近回复去重
            f.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": [f"第{n}个进程在答"], "turn_id": gw.inflight.turn_id})]
            fakes.append(f)
            return f
        gw = Gateway(DEFAULTS, self.data, self.ws, dsh_factory=factory)
        ref[0] = gw
        gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        fakes[0]._alive = False
        out = gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯嗯"})
        self.assertEqual(out["bubbles"], ["第1个进程在答"])
        self.assertEqual(len(fakes), 2)
        self.assertEqual(fakes[0].stopped, 1)
        self.assertIs(gw.dsh, fakes[1])

    def test_initialize_failure_leaves_no_half_dead_client(self):
        self.fake.init_fail_once = True
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertTrue(out["error"].startswith("dsh init failed: "))
        self.assertIsNone(self.gw.dsh)
        self.assertEqual(self.fake.stopped, 1)
        self.assertEqual(self.fake.prompts, [])
        # 下一条正常起来
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["活了"]})]
        out2 = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out2["bubbles"], ["活了"])

    def test_session_id_changes_after_dsh_restart(self):
        # 第一条消息
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["第一轮"]})]
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        first_session_id = self.fake.prompts[0][0]
        self.assertTrue(first_session_id.startswith("K#"))
        # 强制重启
        self.fake._alive = False
        fakes = []
        def factory():
            f = FakeDsh([self.gw])
            f.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["第二轮"]})]
            fakes.append(f)
            return f
        self.gw.dsh_factory = factory
        # 第二条消息
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯嗯"})
        second_session_id = self.fake.prompts[1][0] if len(self.fake.prompts) > 1 else fakes[0].prompts[0][0]
        # 验证：两个 session_id 都以 K# 开头，且互不相同，都不是纯 "K"
        self.assertTrue(first_session_id.startswith("K#"))
        self.assertTrue(second_session_id.startswith("K#"))
        self.assertNotEqual(first_session_id, second_session_id)
        self.assertNotEqual(first_session_id, "K")
        self.assertNotEqual(second_session_id, "K")

    def test_turn_error_reported_not_masked(self):
        self.fake.error_next = True
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        # 应该返回 dsh_error，不是 model_silent
        self.assertEqual(out, {"no_reply": True, "reason": "dsh_error"})
        # 只发了一个 prompt（主提示），没有 nudge
        self.assertEqual(len(self.fake.prompts), 1)
        # 日志里应该包含 dsh_error
        log = self.gw.read_log(1)[0]
        self.assertEqual(log["result"]["reason"], "dsh_error")
        self.assertEqual(log["dsh_error"], "id collision")

    def test_nudge_turn_error_reported_not_masked(self):
        # 首轮模型沉默、nudge 那轮 dsh 报错：也要报 dsh_error，不能伪装成 model_silent
        self.fake.error_at = 2
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out, {"no_reply": True, "reason": "dsh_error"})
        self.assertEqual(len(self.fake.prompts), 2)
        log = self.gw.read_log(1)[0]
        self.assertEqual(log["dsh_error"], "id collision")


if __name__ == "__main__":
    unittest.main()
