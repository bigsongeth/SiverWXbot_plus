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
        self.forwarded = []      # 记录每次 forward 的单个目标
        self.forward_ok = True   # False 模拟视频号等转不了的消息

    def forward(self, target, **kw):
        # 新模型：一次只转一个群（单目标字符串）
        self.forwarded.append(target)
        # 成功 None；失败返回 falsy 的 WxResponse（模拟真实 __bool__）
        return None if self.forward_ok else FakeWxResponse(False, "转发失败")


ZERO_DELAY = {"group_min": 0, "group_max": 0, "batch_every": 10, "batch_min": 0,
              "batch_max": 0, "msg_min": 0, "msg_max": 0, "max_retries": 1}


class FakeWx:
    nickname = "🐶肥肉"

    def __init__(self):
        self.chatted = []
        self.remarks = {}     # 群名 -> 备注（模拟“追加”行为验证幂等）
        self.sent = []

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)

    def GetAllMessage(self):
        return []   # 测试里让 _refresh_collected 回退到原始收集引用

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
        forward._STATE.clear()
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

    # ---------- forward 交互（ncc 主菜单状态机，对齐旧 WCRobot）----------

    def _drain(self):
        """同步执行队列里的群发任务（零延迟）。"""
        while not forward._QUEUE.empty():
            task = forward._QUEUE.get()
            task["delay"] = dict(ZERO_DELAY)
            forward._deliver(task)
            forward._QUEUE.task_done()

    def test_ncc_main_menu(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        m = self.admin.sent[-1]
        self.assertIn("NCC 社群管理", m)
        self.assertIn("1 👈 转发消息", m)
        self.assertIn("0 👈 退出", m)
        self.assertEqual(forward._get_state("大松")["state"], forward.S_MAIN)

    def test_forward_collect_choose_send_flow(self):
        seed_registry()
        # ncc → 主菜单 → 1 进入转发收集
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        self.assertIn("一个一个来", self.admin.sent[-1])
        self.assertEqual(forward._get_state("大松")["state"], forward.S_FWD_COLLECT)
        # 收集两条（静默，不转发、不逐条回复）
        m1 = FakeMsg("[视频号]大曹", mtype="other")   # 视频号也收集
        m2 = FakeMsg("正文", mtype="text")
        before = len(self.admin.sent)
        handle_friend_message(self.bot, self.admin, m1)
        handle_friend_message(self.bot, self.admin, m2)
        self.assertEqual(m1.forwarded, [])
        self.assertEqual(len(self.admin.sent), before)   # 收集阶段静默，无新回复
        self.assertEqual(len(forward._get_state("大松")["messages"]), 2)
        # 发 1 → 汇总 + 选分组菜单（编号 + 所有群聊）
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        self.assertIn("已收集 2 条消息", self.admin.sent[-1])
        self.assertIn("1 👈 所有群聊", self.admin.sent[-1])
        self.assertIn("4 👈 大理群", self.admin.sent[-1])   # 大理群 分组编号=4
        self.assertEqual(forward._get_state("大松")["state"], forward.S_FWD_CHOOSE)
        # 选“4” → 入队 + 状态清除
        handle_friend_message(self.bot, self.admin, FakeMsg("4"))
        self.assertIsNone(forward._get_state("大松"))
        self.assertIn("开始转发 2 条消息到 2 个群", self.admin.sent[-1])
        # 执行队列
        self._drain()
        self.assertEqual(sorted(m1.forwarded), ["大理A群", "大理B群"])
        self.assertEqual(sorted(m2.forwarded), ["大理A群", "大理B群"])
        self.assertIn("转发完成", " ".join(m for _, m in self.bot.wx.sent))

    def test_forward_all_groups_option(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("正文", mtype="text"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))     # 进入选分组
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))     # 1 = 所有群聊
        self.assertIn("开始转发 1 条消息到 2 个群", self.admin.sent[-1])
        self._drain()
        # 完成汇报里含 label「所有群聊」+ 2 个群（大理A/B，C 不允许转发）
        report = " ".join(m for _, m in self.bot.wx.sent)
        self.assertIn("所有群聊", report)
        self.assertIn("2 个群", report)

    def test_forward_multiselect(self):
        # 两个带编号且各有群的分组，多选 4+6
        groupings = {"大理群": {"number": 4, "forward_enabled": True},
                     "黄山群": {"number": 6, "forward_enabled": True}}
        groups = {
            "大A": {"notion_page_id": "1", "allow_forward": True, "allow_speak": True, "welcome_url": "", "groupings": ["大理群"]},
            "黄A": {"notion_page_id": "2", "allow_forward": True, "allow_speak": True, "welcome_url": "", "groupings": ["黄山群"]},
        }
        registry.upsert_from_notion(groupings, groups)
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        m = FakeMsg("正文", mtype="text")
        handle_friend_message(self.bot, self.admin, m)
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("4+6"))   # 多选
        self.assertIn("开始转发 1 条消息到 2 个群", self.admin.sent[-1])
        self._drain()
        self.assertEqual(sorted(m.forwarded), ["大A", "黄A"])

    def test_exit_zero(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("0"))
        self.assertIsNone(forward._get_state("大松"))
        self.assertIn("已退出", self.admin.sent[-1])

    def test_collect_proceed_without_content(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))   # 没收集就想下一步
        self.assertIn("还未收集到任何消息", self.admin.sent[-1])
        self.assertEqual(forward._get_state("大松")["state"], forward.S_FWD_COLLECT)

    def test_sync_via_menu(self):
        called = {}
        orig = notion_sync.pull
        notion_sync.pull = lambda: called.setdefault("v", {"groupings": 12, "groups": 86, "forward_on": 75}) or called["v"]
        try:
            handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
            handle_friend_message(self.bot, self.admin, FakeMsg("2"))   # 菜单里 2=同步
        finally:
            notion_sync.pull = orig
        self.assertTrue(called)
        self.assertIn("同步成功", " ".join(self.admin.sent))

    def test_bare_number_without_state_ignored(self):
        seed_registry()  # 无状态
        handled = handle_friend_message(self.bot, self.admin, FakeMsg("3"))
        self.assertFalse(handled)   # 不劫持普通群聊里的数字

    def test_deliver_one_group_at_a_time(self):
        m = FakeMsg("素材", mtype="text")
        task = {"bot": self.bot, "admin": ADMIN, "messages": [m],
                "targets": [f"群{i}" for i in range(5)], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["ok"], 5)
        self.assertEqual(sorted(m.forwarded), sorted(f"群{i}" for i in range(5)))

    def test_deliver_unforwardable_skipped_after_two_groups(self):
        vid = FakeMsg("[视频号]", mtype="other"); vid.forward_ok = False
        task = {"bot": self.bot, "admin": ADMIN, "messages": [vid],
                "targets": [f"群{i}" for i in range(6)], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["dead"], [0])
        self.assertEqual(len(vid.forwarded), 2)     # 前 2 群试过，后 4 群跳过
        self.assertIn("全程转发失败", " ".join(m for _, m in self.bot.wx.sent))

    def test_direct_sync_shortcut(self):
        called = {}
        orig = notion_sync.pull
        notion_sync.pull = lambda: called.setdefault("v", {"groupings": 12, "groups": 86, "forward_on": 75}) or called["v"]
        try:
            handle_friend_message(self.bot, self.admin, FakeMsg("同步"))
        finally:
            notion_sync.pull = orig
        self.assertTrue(called)
        self.assertIn("86", " ".join(self.admin.sent))

    def test_grouping_list_and_pending(self):
        seed_registry()
        registry.add_pending("某新群")
        handle_friend_message(self.bot, self.admin, FakeMsg("分组列表"))
        self.assertIn("大理群", self.admin.sent[-1])
        handle_friend_message(self.bot, self.admin, FakeMsg("待归类"))
        self.assertIn("某新群", self.admin.sent[-1])

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
