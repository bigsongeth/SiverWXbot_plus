# -*- coding: utf-8 -*-
"""并发回复调度器（plugins/dsh_brain/dispatch.py）：同一会话串行、不同会话并行。

纯 mock：不连微信、不连大脑、不 import wxbot_core。mac 上直接跑文件（见 CLAUDE.md 5.5）。
"""
from __future__ import annotations
import threading
import time
import unittest

from plugins.dsh_brain import dispatch


class FakeConfig:
    def __init__(self, groups=()):
        self.group = list(groups)


class FakeBot:
    def __init__(self, groups=()):
        self.config = FakeConfig(groups)
        self.seen = []
        self.lock = threading.Lock()

    def process_message(self, chat, message):
        with self.lock:
            self.seen.append((chat.who, message, dispatch.in_worker()))


class FakeChat:
    def __init__(self, who, chat_type="group"):
        self.who = who
        self.chat_type = chat_type


class Base(unittest.TestCase):
    def setUp(self):
        dispatch.reset_for_test()
        self._cfg = {"enabled": True, "async_reply": True, "max_workers": 3, "queue_max_per_conv": 5}
        self._orig_load = dispatch.store.load
        self._orig_enabled = dispatch.brain_enabled
        dispatch.store.load = lambda: dict(self._cfg)
        dispatch.brain_enabled = lambda who, is_group: True
        # 别往生产 panel_logs 里写（logger.log 直连面板日志流；test_listen_health.py 踩过同样的坑）
        self.logs = []
        self._orig_log = dispatch._log
        dispatch._log = lambda level, msg: self.logs.append((level, msg))

    def tearDown(self):
        dispatch.store.load = self._orig_load
        dispatch.brain_enabled = self._orig_enabled
        dispatch._log = self._orig_log
        dispatch.reset_for_test()

    def drain(self, timeout=3.0):
        """等所有 worker 收工。"""
        end = time.time() + timeout
        while time.time() < end:
            st = dispatch.stats()
            if st["busy"] == 0 and st["queued"] == 0:
                return True
            time.sleep(0.01)
        return False


class DispatchGateTest(Base):
    def test_async_off_means_no_dispatch(self):
        """总开关关着 = 完全不改变现有行为（合进 main 的安全前提）。"""
        self._cfg["async_reply"] = False
        bot = FakeBot()
        self.assertFalse(dispatch.maybe_dispatch(bot, FakeChat("群A"), "在吗"))
        self.assertEqual(dispatch.stats()["submitted"], 0)

    def test_brain_disabled_conversation_not_dispatched(self):
        """不走大脑的会话（老接口很快）不值得挪线程。"""
        dispatch.brain_enabled = lambda who, is_group: False
        bot = FakeBot()
        self.assertFalse(dispatch.maybe_dispatch(bot, FakeChat("群A"), "在吗"))

    def test_dispatch_runs_in_worker(self):
        bot = FakeBot()
        self.assertTrue(dispatch.maybe_dispatch(bot, FakeChat("群A"), "在吗"))
        self.assertTrue(self.drain())
        self.assertEqual(len(bot.seen), 1)
        self.assertEqual(bot.seen[0][0], "群A")
        self.assertTrue(bot.seen[0][2], "worker 里 in_worker() 必须为 True")

    def test_no_recursive_dispatch(self):
        """★ worker 里回调 process_message 时不能再次派发，否则无限套娃。"""
        bot = FakeBot()
        inner = []

        def reenter(chat, message):
            inner.append(dispatch.maybe_dispatch(bot, chat, message))
        bot.process_message = reenter
        dispatch.maybe_dispatch(bot, FakeChat("群A"), "在吗")
        self.assertTrue(self.drain())
        self.assertEqual(inner, [False])

    def test_missing_who_not_dispatched(self):
        bot = FakeBot()
        self.assertFalse(dispatch.maybe_dispatch(bot, FakeChat(""), "在吗"))

    def test_is_group_by_config_and_chat_type(self):
        bot = FakeBot(groups=["白名单群"])
        self.assertTrue(dispatch.is_group_chat(bot, FakeChat("白名单群", chat_type="friend")))
        self.assertTrue(dispatch.is_group_chat(bot, FakeChat("别的群", chat_type="group")))
        self.assertFalse(dispatch.is_group_chat(bot, FakeChat("松爸", chat_type="friend")))


class OrderingTest(Base):
    def test_same_conversation_is_serialized_and_ordered(self):
        """★ 同一会话严格按到达顺序处理 —— 群里连着两个 @，回复不能乱序。"""
        bot = FakeBot()
        started = threading.Event()
        release = threading.Event()
        order = []

        def slow(chat, message):
            order.append(message)
            if message == "第1条":
                started.set()
                release.wait(2)
        bot.process_message = slow
        for i in (1, 2, 3):
            dispatch.maybe_dispatch(bot, FakeChat("群A"), f"第{i}条")
        self.assertTrue(started.wait(2))
        self.assertEqual(order, ["第1条"])       # 后两条被会话队列挡着
        release.set()
        self.assertTrue(self.drain())
        self.assertEqual(order, ["第1条", "第2条", "第3条"])

    def test_different_conversations_run_in_parallel(self):
        """★ 改造的目的：一个群卡住，别的群照常回。"""
        bot = FakeBot()
        entered = {}
        release = threading.Event()

        def slow(chat, message):
            entered.setdefault(chat.who, threading.Event()).set()
            release.wait(2)
        bot.process_message = slow
        for who in ("群A", "群B", "群C"):
            entered[who] = threading.Event()
            dispatch.maybe_dispatch(bot, FakeChat(who), "在吗")
        for who in ("群A", "群B", "群C"):
            self.assertTrue(entered[who].wait(2), f"{who} 没能同时开跑")
        self.assertEqual(dispatch.stats()["busy"], 3)
        release.set()
        self.assertTrue(self.drain())


class CapacityTest(Base):
    def test_queue_full_drops_oldest(self):
        """积压时丢最旧的：群聊里 5 分钟前的问题早就过时了，回最新的更有意义。"""
        self._cfg["queue_max_per_conv"] = 2
        bot = FakeBot()
        release = threading.Event()
        done = []

        def slow(chat, message):
            if message == "第1条":
                release.wait(2)
            done.append(message)
        bot.process_message = slow
        for i in (1, 2, 3, 4, 5):
            dispatch.maybe_dispatch(bot, FakeChat("群A"), f"第{i}条")
        release.set()
        self.assertTrue(self.drain())
        self.assertGreater(dispatch.stats()["dropped"], 0)
        self.assertIn("第5条", done)             # 最新的一定处理
        self.assertNotIn("第2条", done)          # 最旧的被挤掉

    def test_over_max_workers_queues_but_never_loses(self):
        """★ 名额满时任务留在队列里，有 worker 收工后必须被捡起来（否则会话静默卡死）。"""
        self._cfg["max_workers"] = 1
        bot = FakeBot()
        release = threading.Event()
        done = []

        def slow(chat, message):
            if chat.who == "群A":
                release.wait(2)
            done.append(chat.who)
        bot.process_message = slow
        dispatch.maybe_dispatch(bot, FakeChat("群A"), "在吗")
        time.sleep(0.05)
        dispatch.maybe_dispatch(bot, FakeChat("群B"), "在吗")   # 没名额，排队
        self.assertEqual(dispatch.stats()["busy"], 1)
        release.set()
        self.assertTrue(self.drain())
        self.assertIn("群B", done)               # ★ 被捡起来了，没丢

    def test_task_error_does_not_kill_worker(self):
        """一条消息处理失败不能带崩 worker，否则该会话后面的全卡在队列里。"""
        bot = FakeBot()
        done = []

        def flaky(chat, message):
            if message == "坏的":
                raise RuntimeError("boom")
            done.append(message)
        bot.process_message = flaky
        dispatch.maybe_dispatch(bot, FakeChat("群A"), "坏的")
        dispatch.maybe_dispatch(bot, FakeChat("群A"), "好的")
        self.assertTrue(self.drain())
        self.assertEqual(done, ["好的"])
        self.assertEqual(dispatch.stats()["errors"], 1)
        self.assertTrue(any("boom" in m for _, m in self.logs), self.logs)

    def test_failed_result_is_logged_not_silent(self):
        """异步后 wxbot_core 那条 is_err 告警拿不到真实结果了，失败必须在这里留痕。"""
        bot = FakeBot()
        bot.process_message = lambda chat, message: False
        dispatch.maybe_dispatch(bot, FakeChat("群A"), "在吗")
        self.assertTrue(self.drain())
        self.assertEqual(dispatch.stats()["failed"], 1)
        self.assertTrue(any("返回失败" in m for _, m in self.logs), self.logs)


if __name__ == "__main__":
    unittest.main()
