# -*- coding: utf-8 -*-
"""Phase 1 引擎单测：registry / forward 菜单 / discovery / remark / notion 解析。
全部用假对象，不碰微信、不碰真 Notion。"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from plugins.ncc_community import registry, forward, discovery, remark, notion_sync, store
from plugins.ncc_community import handle_friend_message

ADMIN = "NCC 社群管理肥肉售后维权🤖"
DOG = "\U0001f436"


class FakeChat:
    def __init__(self, who, chat_type="group"):
        self.who = who
        self.chat_type = chat_type
        self.sent = []

    def SendMsg(self, msg=None, **kw):
        self.sent.append(msg)
        return None


class FakeMsg:
    def __init__(self, content="", mtype="text", attr="friend", sender="大松"):
        self.content = content
        self.type = mtype
        self.attr = attr
        self.sender = sender
        self.forwarded = []
        self.forward_ok = True

    def forward(self, targets, **kw):
        self.forwarded.append(list(targets))
        return None if self.forward_ok else {"status": "失败", "message": "找不到"}


class FakeWx:
    nickname = "🐶肥肉"

    def __init__(self):
        self.chatted = []
        self.remarks = {}     # 群名 -> 备注（模拟“追加”行为验证幂等）
        self.sent = []

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)

    def SetGroupRemark(self, value):
        # 模拟 wxautox 追加行为：若已有备注则追加（用于验证我们不会重复设置）
        last = self.chatted[-1] if self.chatted else ""
        self.remarks[last] = self.remarks.get(last, "") + value
        return {"status": "成功", "message": None, "data": None}

    def SendMsg(self, msg=None, who=None, **kw):
        self.sent.append((who, msg))
        return None


class FakeBot:
    def __init__(self):
        self.wx = FakeWx()


def seed_registry():
    """写一个含 2 分组、3 群的登记表。"""
    groupings = {
        "大理群": {"number": 4, "forward_enabled": True},
        "空组": {"number": 9, "forward_enabled": True},
    }
    groups = {
        "大理A群": {"notion_page_id": "p1", "allow_forward": True, "allow_speak": True,
                  "welcome_url": "", "groupings": ["大理群"]},
        "大理B群": {"notion_page_id": "p2", "allow_forward": True, "allow_speak": False,
                  "welcome_url": "", "groupings": ["大理群"]},
        "大理C群": {"notion_page_id": "p3", "allow_forward": False, "allow_speak": True,
                  "welcome_url": "", "groupings": ["大理群"]},  # 不允许转发
    }
    registry.upsert_from_notion(groupings, groups)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncc_eng_")
        # registry 指到临时目录
        self._orig_reg_dir, self._orig_reg_path = registry.DATA_DIR, registry.REGISTRY_PATH
        registry.DATA_DIR = self.tmp
        registry.REGISTRY_PATH = os.path.join(self.tmp, "registry.json")
        # store(config) 指到临时目录
        self._orig_store_dir, self._orig_store_path = store.DATA_DIR, store.CONFIG_PATH
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None
        store._cache_mtime = None
        forward._SESSIONS.clear()
        forward._PENDING_MENU.clear()
        discovery._SEEN.clear()
        self.bot = FakeBot()
        self.admin = FakeChat(ADMIN)

    def tearDown(self):
        registry.DATA_DIR, registry.REGISTRY_PATH = self._orig_reg_dir, self._orig_reg_path
        store.DATA_DIR, store.CONFIG_PATH = self._orig_store_dir, self._orig_store_path
        store._cache = None
        store._cache_mtime = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- registry ----------

    def test_target_uses_name_then_remark(self):
        g = {"name": "群甲", "remark": "群甲🐶", "remark_applied": False}
        self.assertEqual(registry.target(g), "群甲")
        g["remark_applied"] = True
        self.assertEqual(registry.target(g), "群甲🐶")

    def test_targets_for_grouping_filters_allow_forward(self):
        seed_registry()
        data = registry.load()
        t = registry.targets_for_grouping(data, "大理群")
        self.assertEqual(set(t), {"大理A群", "大理B群"})  # C 群 allow_forward=False 被排除

    def test_list_forward_groupings_hides_empty(self):
        seed_registry()
        data = registry.load()
        gs = registry.list_forward_groupings(data)
        names = [n for n, _ in gs]
        self.assertIn("大理群", names)
        self.assertNotIn("空组", names)  # 空组无允许转发的群

    def test_find_by_chat_who_name_and_remark(self):
        seed_registry()
        registry.mark_remark_applied("大理A群", "大理A群🐶")
        data = registry.load()
        n1, _ = registry.find_by_chat_who(data, "大理A群🐶")
        self.assertEqual(n1, "大理A群")
        n2, _ = registry.find_by_chat_who(data, "大理B群")  # 未打备注按名字
        self.assertEqual(n2, "大理B群")

    def test_upsert_preserves_remark_applied(self):
        seed_registry()
        registry.mark_remark_applied("大理A群", "大理A群🐶")
        # 再次同步（模拟 Notion 拉取），remark_applied 应保留
        seed_registry()
        data = registry.load()
        self.assertTrue(data["groups"]["大理A群"]["remark_applied"])

    def test_target_switches_after_remark_applied(self):
        seed_registry()
        registry.mark_remark_applied("大理A群", "大理A群🐶")
        data = registry.load()
        t = registry.targets_for_grouping(data, "大理群")
        self.assertIn("大理A群🐶", t)   # 打了备注 → 按备注寻址
        self.assertNotIn("大理A群", t)

    # ---------- forward 菜单 ----------

    def test_menu_flow(self):
        seed_registry()
        # 发“转发” → 菜单
        handle_friend_message(self.bot, self.admin, FakeMsg("转发"))
        self.assertIn("大理群", self.admin.sent[-1])
        self.assertIn("1.", self.admin.sent[-1])
        # 回“1” → 开始会话
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        self.assertIn("开始转发到「大理群」", self.admin.sent[-1])
        self.assertIsNotNone(forward._get_session("大松"))
        # 发素材 → 转发到 2 个允许转发的群
        mat = FakeMsg("[图片]", mtype="image")
        handle_friend_message(self.bot, self.admin, mat)
        self.assertEqual(set(mat.forwarded[0]), {"大理A群", "大理B群"})
        self.assertIn("已转发到 2 个群", self.admin.sent[-1])
        # 结束
        handle_friend_message(self.bot, self.admin, FakeMsg("结束"))
        self.assertIn("成功 2", self.admin.sent[-1])

    def test_direct_forward_command(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("转发 大理群"))
        self.assertIn("开始转发到「大理群」", self.admin.sent[-1])

    def test_menu_number_out_of_range(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("转发"))
        handle_friend_message(self.bot, self.admin, FakeMsg("9"))
        self.assertIn("超范围", self.admin.sent[-1])

    def test_number_without_menu_ignored_by_forward(self):
        # 没有待选菜单时，数字不该被转发层当选择
        seed_registry()
        handled = handle_friend_message(self.bot, self.admin, FakeMsg("3"))
        self.assertFalse(handled)

    def test_grouping_list_and_pending(self):
        seed_registry()
        registry.add_pending("某新群")
        handle_friend_message(self.bot, self.admin, FakeMsg("分组列表"))
        self.assertIn("大理群", self.admin.sent[-1])
        handle_friend_message(self.bot, self.admin, FakeMsg("待归类"))
        self.assertIn("某新群", self.admin.sent[-1])

    def test_sync_command(self, ):
        # mock notion_sync.pull
        called = {}
        def fake_pull():
            called["yes"] = True
            return {"groupings": 12, "groups": 86, "forward_on": 75}
        orig = notion_sync.pull
        notion_sync.pull = fake_pull
        try:
            handle_friend_message(self.bot, self.admin, FakeMsg("同步"))
        finally:
            notion_sync.pull = orig
        self.assertTrue(called.get("yes"))
        self.assertIn("86", self.admin.sent[-1])

    # ---------- remark ----------

    def test_apply_remark_idempotent(self):
        seed_registry()
        ok, _ = remark.apply_remark(self.bot.wx, "大理A群")
        self.assertTrue(ok)
        self.assertEqual(self.bot.wx.remarks.get("大理A群"), "大理A群🐶")
        # 再次调用不应重复设置（幂等，避免追加）
        self.bot.wx.remarks["大理A群"] = "大理A群🐶"  # reset marker
        ok2, info2 = remark.apply_remark(self.bot.wx, "大理A群")
        self.assertTrue(ok2)
        self.assertIn("跳过", info2)
        self.assertEqual(self.bot.wx.remarks.get("大理A群"), "大理A群🐶")  # 没有被追加

    # ---------- discovery ----------

    def test_discovery_new_group(self):
        seed_registry()
        store.save({"admin_group": ADMIN})
        # mock notion push
        pushed = {}
        orig = notion_sync.push_discovery
        notion_sync.push_discovery = lambda name: pushed.setdefault("name", name)
        try:
            new_chat = FakeChat("野生新群")
            handle_friend_message(self.bot, new_chat, FakeMsg("大家好", sender="路人"))
        finally:
            notion_sync.push_discovery = orig
        data = registry.load()
        self.assertIn("野生新群", data["groups"])
        self.assertEqual(data["groups"]["野生新群"]["status"], "pending")
        self.assertTrue(data["groups"]["野生新群"]["remark_applied"])   # 自动打了备注
        self.assertEqual(pushed.get("name"), "野生新群")                # 推了 Notion
        # 管理群收到提醒
        self.assertTrue(any("发现新群" in (m or "") for _, m in self.bot.wx.sent))

    def test_discovery_known_group_no_repush(self):
        seed_registry()
        store.save({"admin_group": ADMIN})
        pushed = []
        orig = notion_sync.push_discovery
        notion_sync.push_discovery = lambda name: pushed.append(name)
        try:
            handle_friend_message(self.bot, FakeChat("大理A群"), FakeMsg("hi", sender="a"))
        finally:
            notion_sync.push_discovery = orig
        self.assertEqual(pushed, [])  # 已登记群不重复推送

    def test_discovery_ignores_admin_and_private(self):
        seed_registry()
        store.save({"admin_group": ADMIN})
        orig = notion_sync.push_discovery
        notion_sync.push_discovery = lambda name: (_ for _ in ()).throw(AssertionError("不该推送"))
        try:
            handle_friend_message(self.bot, FakeChat("私聊对象", chat_type="friend"),
                                  FakeMsg("hi", sender="私聊对象"))
        finally:
            notion_sync.push_discovery = orig
        data = registry.load()
        self.assertNotIn("私聊对象", data["groups"])

    # ---------- notion 解析 ----------

    def test_parse_notion(self):
        grouping_rows = [{
            "id": "g4",
            "properties": {"组名": {"title": [{"plain_text": "大理群"}]},
                           "分组编号": {"number": 4}, "是否转发": {"checkbox": True}},
        }]
        group_rows = [{
            "id": "row1",
            "properties": {
                "群名": {"title": [{"plain_text": "NCC大理一家人"}]},
                "允许转发": {"checkbox": True},
                "允许发言": {"checkbox": False},
                "迎新推送链接（填写后视为开启）": {"url": "https://x"},
                "转发群聊分组": {"relation": [{"id": "g4"}]},
            },
        }]
        groupings, groups = notion_sync.parse_notion(group_rows, grouping_rows)
        self.assertEqual(groupings["大理群"]["number"], 4)
        self.assertTrue(groupings["大理群"]["forward_enabled"])
        g = groups["NCC大理一家人"]
        self.assertTrue(g["allow_forward"])
        self.assertFalse(g["allow_speak"])
        self.assertEqual(g["welcome_url"], "https://x")
        self.assertEqual(g["groupings"], ["大理群"])


if __name__ == "__main__":
    unittest.main()
