# -*- coding: utf-8 -*-
"""Phase 3 批量纳管单测：preview / _apply_one / 指令路由 / notion 标题标记。"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest

from plugins.ncc_community import registry, batch, notion_sync, store

ADMIN = "NCC 社群管理肥肉售后维权🤖"
DOG = "\U0001f436"


class FakeChat:
    def __init__(self, who=ADMIN):
        self.who = who
        self.sent = []

    def SendMsg(self, msg=None, **kw):
        self.sent.append(msg)
        return None


class FakeWx:
    nickname = "🐶肥肉"

    def __init__(self, types=None):
        self.types = types or {}      # 群名 -> chat_type
        self.chatted = []
        self.remarks = {}
        self.sent = []

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)
        self._cur = who

    def ChatInfo(self):
        # 真机上没有 remark 字段，且群有备注后 chat_name 显示的就是备注本身
        return {"chat_type": self.types.get(self._cur, "group"),
                "chat_name": self.remarks.get(self._cur) or self._cur}

    def SetGroupRemark(self, value):
        self.remarks[self._cur] = self.remarks.get(self._cur, "") + value
        return {"status": "成功", "message": None, "data": None}

    def SendMsg(self, msg=None, who=None, **kw):
        self.sent.append((who, msg))
        return None


class FakeBot:
    def __init__(self, types=None):
        self.wx = FakeWx(types)


def seed(names_groupings=None):
    groupings = {"大理群": {"number": 4, "forward_enabled": True}}
    groups = {}
    for n in (names_groupings or ["群甲", "群乙", "群丙"]):
        groups[n] = {"notion_page_id": "pg_" + n, "allow_forward": True,
                     "allow_speak": True, "welcome_url": "", "groupings": ["大理群"]}
    registry.upsert_from_notion(groupings, groups)


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncc_batch_")
        self._r = (registry.DATA_DIR, registry.REGISTRY_PATH)
        registry.DATA_DIR = self.tmp
        registry.REGISTRY_PATH = os.path.join(self.tmp, "registry.json")
        self._s = (store.DATA_DIR, store.CONFIG_PATH)
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None; store._cache_mtime = None

    def tearDown(self):
        registry.DATA_DIR, registry.REGISTRY_PATH = self._r
        store.DATA_DIR, store.CONFIG_PATH = self._s
        store._cache = None; store._cache_mtime = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- preview ----------

    def test_preview_split(self):
        seed()
        registry.mark_remark_applied("群甲", "群甲🐶")
        p = batch.preview()
        self.assertEqual(p["done_n"], 1)
        self.assertEqual(p["todo_n"], 2)
        self.assertIn("群甲", p["done"])

    def test_format_preview_text(self):
        seed()
        txt = batch.format_preview()
        self.assertIn("待打 3", txt)
        self.assertIn("群甲", txt)

    # ---------- _apply_one ----------

    def test_apply_one_group(self):
        seed()
        wx = FakeWx({"群甲": "group"})
        status, info = batch._apply_one(wx, "群甲", threading.Lock())
        self.assertEqual(status, "ok")
        self.assertEqual(wx.remarks["群甲"], "群甲🐶")       # 干净单次设置
        data = registry.load()
        self.assertTrue(data["groups"]["群甲"]["remark_applied"])

    def test_apply_one_skips_person(self):
        seed(["某人"])
        wx = FakeWx({"某人": "friend"})       # 是个人
        status, info = batch._apply_one(wx, "某人", threading.Lock())
        self.assertEqual(status, "skip")
        self.assertNotIn("某人", wx.remarks)   # 没给个人打备注
        data = registry.load()
        self.assertFalse(data["groups"]["某人"]["remark_applied"])

    def test_apply_one_skips_when_window_is_another_group(self):
        """模糊搜索命中了名字相近的另一个群 → 绝不能在它头上打备注（备注是追加、清不掉）。"""
        seed(["群甲"])

        class DriftWx(FakeWx):
            def ChatWith(self, who=None, exact=False):
                self.chatted.append(who)
                self._cur = "另一个群"        # 切歪了

        wx = DriftWx({"群甲": "group", "另一个群": "group"})
        status, info = batch._apply_one(wx, "群甲", threading.Lock())
        self.assertEqual(status, "skip")
        self.assertEqual(wx.remarks, {})       # 谁的备注都没动
        self.assertIn("另一个群", info)
        self.assertFalse(registry.load()["groups"]["群甲"]["remark_applied"])

    def test_apply_one_idempotent(self):
        seed()
        registry.mark_remark_applied("群甲", "群甲🐶")
        wx = FakeWx({"群甲": "group"})
        status, info = batch._apply_one(wx, "群甲", threading.Lock())
        self.assertEqual(status, "ok")
        self.assertEqual(wx.remarks, {})       # 已打过，不重设（避免追加）

    def test_remark_worker_end_to_end(self):
        seed(["群甲", "群乙", "个人X"])
        bot = FakeBot({"群甲": "group", "群乙": "group", "个人X": "friend"})
        batch._RUNNING.acquire()
        try:
            batch._remark_worker(bot, ADMIN, limit=0)
        finally:
            pass  # worker 的 finally 会 release
        data = registry.load()
        self.assertTrue(data["groups"]["群甲"]["remark_applied"])
        self.assertTrue(data["groups"]["群乙"]["remark_applied"])
        self.assertFalse(data["groups"]["个人X"]["remark_applied"])
        summary = "\n".join(m for _, m in bot.wx.sent)
        self.assertIn("成功 2", summary)
        self.assertIn("跳过 1", summary)

    # ---------- 指令路由 ----------

    def test_command_preview(self):
        seed()
        chat = FakeChat()
        handled = batch.handle_batch_command(FakeBot(), chat, {"admin_group": ADMIN}, "批量备注预览")
        self.assertTrue(handled)
        self.assertIn("待打 3", chat.sent[-1])

    def test_command_limit_routing(self):
        seed()
        calls = {}
        orig = batch.run_remark_pass
        batch.run_remark_pass = lambda bot, admin, limit=0: calls.setdefault("limit", limit)
        try:
            handled = batch.handle_batch_command(FakeBot(), FakeChat(), {"admin_group": ADMIN}, "批量备注 5")
        finally:
            batch.run_remark_pass = orig
        self.assertTrue(handled)
        self.assertEqual(calls["limit"], 5)

    def test_command_notion_writeback_retired(self):
        """「回写notion」已下线（去 Notion 化），但仍接住并回一句人话，
        且不能再有任何 Notion 调用。"""
        chat = FakeChat()
        handled = batch.handle_batch_command(FakeBot(), chat, {"admin_group": ADMIN}, "回写Notion")
        self.assertTrue(handled)
        self.assertFalse(hasattr(batch, "run_notion_pass"))
        self.assertIn("下线", chat.sent[-1])
        self.assertIn("/ncc_community", chat.sent[-1])

    def test_command_miss(self):
        self.assertFalse(batch.handle_batch_command(FakeBot(), FakeChat(), {"admin_group": ADMIN}, "随便说句话"))

    # ---------- notion 标题🐶标记 ----------

    def test_strip_dog(self):
        self.assertEqual(notion_sync._strip_dog("群名🐶"), ("群名", True))
        self.assertEqual(notion_sync._strip_dog("群名"), ("群名", False))

    def test_parse_notion_marks_dog_title(self):
        grouping_rows = [{"id": "g4", "properties": {
            "组名": {"title": [{"plain_text": "大理群"}]},
            "分组编号": {"number": 4}, "是否转发": {"checkbox": True}}}]
        group_rows = [{"id": "r1", "properties": {
            "群名": {"title": [{"plain_text": "大理一家人🐶"}]},
            "允许转发": {"checkbox": True}, "允许发言": {"checkbox": True},
            "迎新推送链接（填写后视为开启）": {"url": ""},
            "转发群聊分组": {"relation": [{"id": "g4"}]}}}]
        _, groups = notion_sync.parse_notion(group_rows, grouping_rows)
        self.assertIn("大理一家人", groups)                  # 剥掉🐶
        self.assertTrue(groups["大理一家人"]["notion_marked"])

    def test_upsert_ignores_notion_marked(self):
        """Notion🐶 不再顶 remark_applied（PANEL_SPEC §1 #7，假绿来源）。
        remark_applied 只认本地实打实打成功过的那一笔。"""
        groupings = {"大理群": {"number": 4, "forward_enabled": True}}
        groups = {"大理一家人": {"notion_page_id": "r1", "allow_forward": True,
                             "allow_speak": True, "welcome_url": "", "groupings": ["大理群"],
                             "notion_marked": True}}
        registry.upsert_from_notion(groupings, groups)
        data = registry.load()
        self.assertFalse(data["groups"]["大理一家人"]["remark_applied"])


if __name__ == "__main__":
    unittest.main()
