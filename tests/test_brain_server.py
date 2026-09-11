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


def only(gw):
    """取唯一在飞的那一轮。inflight 2026-09-08 起是 {turn_id: Inflight}（并发改造）。"""
    vals = list(gw.inflight.values())
    assert len(vals) == 1, "期望恰好一轮在飞，实际 %d" % len(vals)
    return vals[0]


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

    def test_silent_model_gets_nudge_then_error(self):
        # nudge 完仍不调工具 = 大脑失灵，报 error 让机器人换老接口顶上，不能伪装成「不接话」
        self.fake.script = [lambda gw: None, lambda gw: None]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertNotIn("no_reply", out); self.assertEqual(out["error"], "model_silent")
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn("wx_reply", self.fake.prompts[1][1])

    def test_history_every_turn(self):
        # 首轮整段预热；之后每轮只带上次之后的新消息（含卡片），旧的不重复灌。用户 2026-09-06：历史必须带
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["在"]}),
                            lambda gw: gw.tool_call("wx_reply", {"bubbles": ["还在"]}),
                            lambda gw: gw.tool_call("wx_reply", {"bubbles": ["收了"]})]
        prime = [{"time": "t1", "attr": "friend", "sender": "K", "content": "早", "type": "text"}]
        self.gw.handle_reply({"conversation": "G", "is_group": True, "sender": "K", "text": "在吗", "prime": prime})
        self.gw.handle_reply({"conversation": "G", "is_group": True, "sender": "K", "text": "还在吗", "prime": prime})
        card = {"time": "t2", "attr": "friend", "sender": "鹅", "type": "miniapp",
                "content": "小程序大众点评美食电影运动旅游门票潮汕菜大排档"}
        self.gw.handle_reply({"conversation": "G", "is_group": True, "sender": "K", "text": "收一下上面那家", "prime": prime + [card]})
        p0, p1, p2 = (p[1] for p in self.fake.prompts)
        self.assertIn("此前的聊天记录", p0); self.assertIn("K: 早", p0)
        self.assertNotIn("聊天记录", p1); self.assertNotIn("新出现的消息", p1); self.assertNotIn("K: 早", p1)
        self.assertIn("上一轮之后", p2); self.assertIn("[大众点评卡片] 潮汕菜大排档", p2); self.assertNotIn("K: 早", p2)

    def test_delta_not_marked_seen_when_dsh_errors(self):
        # dsh 那轮报错，模型没看到；下一轮要把这些新消息再带一遍
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["在"]}), lambda gw: None,
                            lambda gw: gw.tool_call("wx_reply", {"bubbles": ["收了"]})]
        prime = [{"time": "t1", "attr": "friend", "sender": "K", "content": "早", "type": "text"}]
        card = {"time": "t2", "attr": "friend", "sender": "鹅", "type": "miniapp", "content": "小程序美团外卖丨外卖美食奶茶咖啡水果凡老头米线砂锅"}
        self.gw.handle_reply({"conversation": "G", "is_group": True, "sender": "K", "text": "在吗", "prime": prime})
        self.fake.error_next = True
        out = self.gw.handle_reply({"conversation": "G", "is_group": True, "sender": "K", "text": "收一下", "prime": prime + [card]})
        self.assertEqual(out.get("error"), "dsh_error")
        self.gw.handle_reply({"conversation": "G", "is_group": True, "sender": "K", "text": "再收一下", "prime": prime + [card]})
        self.assertIn("[美团外卖卡片] 凡老头米线砂锅", self.fake.prompts[-1][1])

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
            ids.append(only(gw).turn_id)
            gw.tool_call("wx_reply", {"bubbles": ["第一轮"], "turn_id": only(gw).turn_id})

        def second(gw):
            r = gw.tool_call("wx_reply", {"bubbles": ["串话"], "turn_id": ids[0]})
            assert r["ok"] is False, r
            r2 = gw.tool_call("wx_reply", {"bubbles": ["正确"], "turn_id": only(gw).turn_id})
            assert r2["ok"], r2
        self.fake.script = [first, second]
        self.gw.handle_reply({"conversation": "A", "is_group": False, "sender": "A", "text": "在吗"})
        out = self.gw.handle_reply({"conversation": "B", "is_group": False, "sender": "B", "text": "你好"})
        self.assertEqual(out["bubbles"], ["正确"])
        self.assertNotEqual(ids[0], self.fake.prompts[1][1])  # 两轮 id 不同

    def test_prefix_line_carries_turn_id(self):
        ids = []
        self.fake.script = [lambda gw: (ids.append(only(gw).turn_id),
                                        gw.tool_call("wx_reply", {"bubbles": ["在"], "turn_id": only(gw).turn_id}))]
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "在吗"})
        first_line = self.fake.prompts[0][1].split("\n")[0]
        self.assertIn("| 轮次:" + ids[0] + "]", first_line)
        self.assertTrue(first_line.startswith("[群聊:K | 发言人:K"))

    def test_missing_turn_id_accepted_but_logged(self):
        seen = []
        self.fake.script = [lambda gw: (gw.tool_call("wx_reply", {"bubbles": ["无标识"]}),
                                        seen.extend(only(gw).tool_log))]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out["bubbles"], ["无标识"])
        self.assertIn({"tool": "wx_reply", "no_turn_id": True}, seen)

    def test_retry_attempt_adds_hint(self):
        """机器人侧重试（attempt>1）那轮，消息末尾带「少调工具」提示；首轮没有。"""
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["好"]})] * 2
        self.gw.handle_reply({"conversation": "肥肉测试1🐶", "is_group": True, "sender": "松爸", "text": "收一下"})
        self.assertNotIn("系统提示", self.fake.prompts[0][1])
        self.gw.handle_reply({"conversation": "肥肉测试1🐶", "is_group": True, "sender": "松爸", "text": "收一下", "attempt": 2})
        self.assertIn("第 2 次尝试", self.fake.prompts[1][1])
        self.assertIn("同一个工具报错 2 次就停", self.fake.prompts[1][1])
        self.assertEqual(self.gw.read_log(1)[0]["attempt"], 2)

    def test_single_timeout_keeps_dsh_alive(self):
        """并发改造后：一次超时不再杀进程（会误杀别人在飞的轮次），迟到回调靠 turn_id 挡。"""
        self.fake.timeout_next = True
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out, {"error": "timeout", "reason": "timeout"})
        self.assertEqual(self.fake.stopped, 0)
        self.assertIsNotNone(self.gw.dsh)
        self.assertEqual(len(self.fake.prompts), 1)  # 超时后不再 nudge
        self.assertFalse(self.gw.read_log(1)[0]["dsh_restarted_after_timeout"])

    def test_consecutive_timeouts_restart_dsh(self):
        """连续 restart_after_timeouts(默认3) 轮超时才重建：不是个别轮次的事了。"""
        self.fake.timeout_next = True
        for _ in range(2):
            self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
            self.assertEqual(self.fake.stopped, 0)
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(self.fake.stopped, 1)
        self.assertIsNone(self.gw.dsh)
        self.assertTrue(self.gw.read_log(1)[0]["dsh_restarted_after_timeout"])

    def test_success_resets_timeout_streak(self):
        """中间成功一轮，连续超时计数清零 —— 否则零星超时攒够 3 次也会误重建。"""
        self.fake.timeout_next = True
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.fake.timeout_next = False
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["好"]})]
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(self.gw._timeout_streak, 0)
        self.fake.timeout_next = True
        self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(self.fake.stopped, 0)   # 重新数，还没到 3

    def test_ensure_dsh_stops_old_client(self):
        ref = [None]
        fakes = []

        def factory():
            f = FakeDsh(ref)
            n = len(fakes)   # 两次文案不同，避免撞上同会话的最近回复去重
            f.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": [f"第{n}个进程在答"], "turn_id": only(gw).turn_id})]
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
        # 应该返回 dsh_error，不是 model_silent；而且是 error 不是 no_reply（额度耗尽/key 失效要能走故障转移）
        self.assertEqual(out, {"error": "dsh_error", "reason": "dsh_error"})
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
        self.assertEqual(out, {"error": "dsh_error", "reason": "dsh_error"})
        self.assertEqual(len(self.fake.prompts), 2)
        log = self.gw.read_log(1)[0]
        self.assertEqual(log["dsh_error"], "id collision")


# ============================================================
# 并发（2026-09-08 改造）：同一会话串行、不同会话并行
# 设计见 docs/superpowers/specs/2026-09-08-concurrency-design.md
# ============================================================
import re          # noqa: E402
import threading   # noqa: E402
import time        # noqa: E402

TURN_RE = re.compile(r"轮次:([0-9a-f]+)\]")


class BlockingDsh:
    """prompt 会卡在那儿等测试放行 —— 用来把两轮「按住」在同时在飞的状态上。

    真实 dsh 一轮要几十秒，单测不能靠 sleep 去撞时序；这里用 Event 精确控制
    「进入了 prompt」和「允许返回」两个时刻，跑得快而且不会偶发。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.entered = {}          # conv -> Event（该会话进了 prompt）
        self.release = {}          # conv -> Event（放它返回）
        self.prompts = []
        self.reply_in_prompt = True   # 在 prompt 里替模型调 wx_reply
        self.omit_turn_id = set()     # 这些会话调 wx_reply 时故意不带 turn_id
        self.timeout_convs = set()    # 这些会话直接超时
        self.tool_results = {}        # conv -> 该会话那次 wx_reply 的返回（按会话分开，别互相覆盖）
        self.gw = None
        self._alive = True
        self.stopped = 0

    def _ev(self, d, conv):
        with self.lock:
            if conv not in d:
                d[conv] = threading.Event()
            return d[conv]

    def entered_ev(self, conv):
        return self._ev(self.entered, conv)

    def release_ev(self, conv):
        return self._ev(self.release, conv)

    def start(self):
        self._alive = True

    def initialize(self, cwd, provider, model):
        return {}

    def alive(self):
        return self._alive

    def stop(self):
        self._alive = False
        with self.lock:
            self.stopped += 1

    def prompt(self, session_id, text, timeout_sec):
        conv = session_id.split("#")[0]
        with self.lock:
            self.prompts.append((session_id, text))
        self.entered_ev(conv).set()
        self.release_ev(conv).wait(timeout=5)
        if conv in self.timeout_convs:
            return TurnResult(reasoning="卡住了", timed_out=True)
        if self.reply_in_prompt:
            m = TURN_RE.search(text)
            args = {"bubbles": ["来自" + conv]}
            if m and conv not in self.omit_turn_id:
                args["turn_id"] = m.group(1)
            self.tool_results[conv] = self.gw.tool_call("wx_reply", args)
        return TurnResult(reasoning="想了一下", events=[{"type": "turn/end"}])


class ConcurrentGatewayTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.ws = os.path.join(self.data, "workspace")
        os.makedirs(os.path.join(self.ws, "knowledge"))
        os.makedirs(os.path.join(self.ws, "memory", "people"))
        with open(os.path.join(self.ws, "knowledge", "shared.md"), "w", encoding="utf-8") as f:
            f.write("# 共享知识\n")
        os.makedirs(os.path.join(self.ws, "skills"))
        with open(os.path.join(self.ws, "skills", "index.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        self.dsh = BlockingDsh()

    def _gw(self, **over):
        cfg = dict(DEFAULTS)
        cfg.update(over)
        gw = Gateway(cfg, self.data, self.ws, dsh_factory=lambda: self.dsh)
        self.dsh.gw = gw
        return gw

    def _fire(self, gw, conv, out, **extra):
        payload = {"conversation": conv, "is_group": True, "sender": "松爸", "text": "在吗"}
        payload.update(extra)
        th = threading.Thread(target=lambda: out.update({conv: gw.handle_reply(payload)}))
        th.start()
        return th

    def test_two_conversations_run_concurrently(self):
        """两个会话能同时在飞 —— 这是整个改造的目的。"""
        gw = self._gw()
        out = {}
        t1 = self._fire(gw, "群A", out)
        t2 = self._fire(gw, "群B", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3), "群A 没进 prompt")
        self.assertTrue(self.dsh.entered_ev("群B").wait(3), "群B 没进 prompt")
        self.assertEqual(len(gw.inflight), 2)      # ★ 两轮同时在飞
        self.dsh.release_ev("群A").set()
        self.dsh.release_ev("群B").set()
        t1.join(5); t2.join(5)
        self.assertEqual(out["群A"]["bubbles"], ["来自群A"])
        self.assertEqual(out["群B"]["bubbles"], ["来自群B"])   # ★ 没串台
        self.assertEqual(len(gw.inflight), 0)

    def test_same_conversation_serialized(self):
        """同一会话仍严格串行：保序，且 primed/seen 状态不打架。"""
        gw = self._gw()
        out = {}
        t1 = self._fire(gw, "群A", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3))
        t2 = threading.Thread(target=lambda: out.setdefault("第二条", gw.handle_reply(
            {"conversation": "群A", "is_group": True, "sender": "松爸", "text": "再问一句"})))
        t2.start()
        time.sleep(0.2)
        self.assertEqual(len(gw.inflight), 1)      # ★ 第二条被会话锁挡在外面
        self.dsh.release_ev("群A").set()
        t1.join(5); t2.join(5)
        # 两条都处理了，只是排队（条数不写死：第二条内容重复会触发 validate 去重 → 多一轮 nudge）
        texts = [t for _, t in self.dsh.prompts]
        self.assertTrue(any("在吗" in t for t in texts), texts)
        self.assertTrue(any("再问一句" in t for t in texts), texts)

    def test_over_max_concurrent_returns_busy(self):
        """名额满了返回 busy（HTTP 503），机器人侧照旧重试。"""
        gw = self._gw(max_concurrent=1, lock_timeout_sec=0.3)
        out = {}
        t1 = self._fire(gw, "群A", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3))
        self.assertEqual(gw.handle_reply({"conversation": "群B", "is_group": True,
                                          "sender": "松爸", "text": "在吗"}), {"error": "busy"})
        self.dsh.release_ev("群A").set()
        t1.join(5)

    def test_max_concurrent_one_is_old_serial_behaviour(self):
        """max_concurrent=1 = 退回改造前的全局串行，这是出事时的回滚开关。"""
        gw = self._gw(max_concurrent=1)
        out = {}
        t1 = self._fire(gw, "群A", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3))
        t2 = self._fire(gw, "群B", out)
        time.sleep(0.2)
        self.assertEqual(len(gw.inflight), 1)      # 群B 拿不到名额，等着
        self.dsh.release_ev("群A").set()
        self.dsh.release_ev("群B").set()
        t1.join(5); t2.join(5)

    def test_missing_turn_id_rejected_when_multiple_inflight(self):
        """★ 并发串台的正门：在飞 ≥2 轮时，不带 turn_id 的收尾调用必须拒。"""
        gw = self._gw()
        self.dsh.omit_turn_id = {"群A"}
        out = {}
        t1 = self._fire(gw, "群A", out)
        t2 = self._fire(gw, "群B", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3))
        self.assertTrue(self.dsh.entered_ev("群B").wait(3))
        self.assertEqual(len(gw.inflight), 2)
        self.dsh.release_ev("群A").set()
        t1.join(5)
        self.dsh.release_ev("群B").set()
        t2.join(5)
        # 群A 没带 turn_id 又赶上多轮在飞 → 被拒 → 它这轮没有 result
        self.assertIn("无法确定它属于哪一条", self.dsh.tool_results["群A"]["text"])
        self.assertFalse(self.dsh.tool_results["群A"]["ok"])
        self.assertEqual(out["群B"]["bubbles"], ["来自群B"])   # 群B 不受影响

    def test_stale_turn_id_cannot_hit_another_inflight(self):
        """拿一个不存在的 turn_id 打过来，不能落到别人的槽里。"""
        gw = self._gw()
        out = {}
        t1 = self._fire(gw, "群A", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3))
        r = gw.tool_call("wx_reply", {"bubbles": ["串话"], "turn_id": "deadbeef"})
        self.assertFalse(r["ok"])
        self.dsh.release_ev("群A").set()
        t1.join(5)
        self.assertEqual(out["群A"]["bubbles"], ["来自群A"])

    def test_timeout_does_not_kill_other_inflight(self):
        """★ 一个会话超时，不能把别人正在跑的轮次连坐杀掉。"""
        gw = self._gw(restart_after_timeouts=1)   # 一超时就够格重建，但别人在飞就得让路
        self.dsh.timeout_convs = {"群A"}
        out = {}
        t1 = self._fire(gw, "群A", out)
        t2 = self._fire(gw, "群B", out)
        self.assertTrue(self.dsh.entered_ev("群A").wait(3))
        self.assertTrue(self.dsh.entered_ev("群B").wait(3))
        self.dsh.release_ev("群A").set()
        t1.join(5)
        self.assertEqual(self.dsh.stopped, 0)      # ★ 群B 还在飞，没杀进程
        self.assertTrue(gw.dsh.alive())
        self.dsh.release_ev("群B").set()
        t2.join(5)
        self.assertEqual(out["群B"]["bubbles"], ["来自群B"])   # 群B 正常收尾


if __name__ == "__main__":
    unittest.main()
