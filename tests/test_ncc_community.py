# -*- coding: utf-8 -*-
"""ncc_community 插件单元测试（不依赖微信环境，全部用假对象）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from plugins.ncc_community import (store, forward, invite, registry, welcome, notion_sync,
                                   audit, remark, task_runner)
from plugins.ncc_community import handle_friend_message, handle_self_message, handle_system_message
from plugins.ncc_community.common import REPLY_PREFIX


ADMIN_GROUP = "NCC 社群管理肥肉售后维权🤖"


class FakeWxResponse:
    def __init__(self, ok=True, message=""):
        self.ok = ok
        self.message = message

    def __bool__(self):
        return self.ok

    def __getitem__(self, key):
        return {"message": self.message}.get(key)


class FakeChat:
    def __init__(self, who, chat_type="group"):
        self.who = who
        self.chat_type = chat_type
        self.sent = []

    def SendMsg(self, msg=None, **kwargs):
        self.sent.append(msg)
        return FakeWxResponse(True)


class FakeMsg:
    def __init__(self, content="", mtype="text", attr="friend", sender="大松"):
        self.content = content
        self.type = mtype
        self.attr = attr
        self.sender = sender
        self.forwarded_to = []
        self.forward_ok = True

    def forward(self, targets, **kwargs):
        self.forwarded_to.append(list(targets))
        # 真实 wxautox4 成功时返回 None，失败时返回 falsy WxResponse 或抛异常
        if self.forward_ok:
            return None
        return FakeWxResponse(False, "找不到聊天对象")


class FakeWx:
    nickname = "肥肉"

    def __init__(self):
        self.chatted = []
        self.added_members = []
        self.url_cards = []
        self.add_result = FakeWxResponse(True)
        self.sent_to = []
        # 切不过去的会话名（模拟 ChatWith 静默失败：返回 falsy，窗口留在原处）
        self.fail_names = set()
        # 会话名 -> chat_type，缺省当群聊
        self.chat_types = {}
        # 当前主窗口停在哪个会话
        self.current_chat = None

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)
        if who in self.fail_names:
            return FakeWxResponse(False, "未找到会话")
        self.current_chat = who
        return None          # 真实 wxautox 成功时可能返回 None

    def ChatInfo(self):
        if self.current_chat is None:
            return {}
        return {"chat_name": self.current_chat,
                "chat_type": self.chat_types.get(self.current_chat, "group"),
                "remark": ""}

    def AddGroupMembers(self, members=None, **kwargs):
        self.added_members.append(list(members))
        return self.add_result

    def SendMsg(self, msg=None, who=None, **kwargs):
        self.sent_to.append((who, msg))
        return FakeWxResponse(True)

    def SendUrlCard(self, url=None, friends=None, **kwargs):
        self.url_cards.append((url, friends))
        return FakeWxResponse(True)


class FakeBotConfig:
    AtMe = "@肥肉"
    group = [ADMIN_GROUP, "肥肉测试1", "爱和一切肥肉测试群"]


class FakeBot:
    def __init__(self):
        self.wx = FakeWx()
        self.config = FakeBotConfig()


class NccCommunityTestCase(unittest.TestCase):
    def setUp(self):
        # 把插件配置指到临时目录，避免污染真实 data/
        self.tmpdir = tempfile.mkdtemp(prefix="ncc_test_")
        self._orig_data_dir = store.DATA_DIR
        self._orig_config_path = store.CONFIG_PATH
        store.DATA_DIR = self.tmpdir
        store.CONFIG_PATH = os.path.join(self.tmpdir, "config.json")
        store._cache = None
        store._cache_mtime = None
        self._orig_reg_dir = registry.DATA_DIR
        self._orig_reg_path = registry.REGISTRY_PATH
        registry.DATA_DIR = self.tmpdir
        registry.REGISTRY_PATH = os.path.join(self.tmpdir, "registry.json")
        forward._STATE.clear()
        invite._QUOTA.clear()
        invite._FAILS.clear()
        # 切群重试的等待在单测里没意义，清零省时间
        self._orig_waits = (invite._SWITCH_WAIT, invite._SETTLE_AFTER_SWITCH)
        invite._SWITCH_WAIT = 0
        invite._SETTLE_AFTER_SWITCH = 0
        self.bot = FakeBot()
        self.admin_chat = FakeChat(ADMIN_GROUP)

    def tearDown(self):
        invite._SWITCH_WAIT, invite._SETTLE_AFTER_SWITCH = self._orig_waits
        store.DATA_DIR = self._orig_data_dir
        store.CONFIG_PATH = self._orig_config_path
        store._cache = None
        store._cache_mtime = None
        registry.DATA_DIR = self._orig_reg_dir
        registry.REGISTRY_PATH = self._orig_reg_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- store ----------

    def test_store_creates_default_config(self):
        cfg = store.load()
        self.assertEqual(cfg["admin_group"], ADMIN_GROUP)
        self.assertTrue(os.path.exists(store.CONFIG_PATH))

    def test_store_save_and_reload(self):
        cfg = store.load()
        cfg["forward"]["groups"]["新分组"] = ["群A"]
        store.save(cfg)
        store._cache = None
        cfg2 = store.load()
        self.assertEqual(cfg2["forward"]["groups"]["新分组"], ["群A"])

    # ---------- forward: 指令 ----------

    def test_help_command(self):
        handled = handle_friend_message(self.bot, self.admin_chat, FakeMsg("帮助"))
        self.assertTrue(handled)
        self.assertIn("转发", self.admin_chat.sent[0])

    # Phase1 的转发流程用例（转发 <组名> / _get_session / config 分组）已随
    # Phase3 重构删除，现行菜单式转发流程的测试在 tests/test_ncc_engine.py。

    def test_sessions_isolated_by_sender(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("转发 测试组", sender="大松"))
        # 另一个管理员的普通消息不该被转发
        other = FakeMsg("随便聊聊", sender="大曹")
        handled = handle_friend_message(self.bot, self.admin_chat, other)
        self.assertFalse(handled)
        self.assertEqual(other.forwarded_to, [])

    def test_bot_reply_ignored(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("转发 测试组"))
        bot_reply = FakeMsg(f"{REPLY_PREFIX} 已转发到 2 个群 ✅")
        handled = handle_friend_message(self.bot, self.admin_chat, bot_reply)
        self.assertFalse(handled)
        self.assertEqual(bot_reply.forwarded_to, [])

    def test_self_message_command(self):
        # 机器人账号自己（手机端）发的指令也生效
        msg = FakeMsg("分组列表", attr="self", sender="肥肉")
        handled = handle_self_message(self.bot, self.admin_chat, msg)
        self.assertTrue(handled)

    # ---------- 拉群 / 迎新配置指令 ----------

    def test_invite_keyword_management(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("设拉群 灵感食堂|灵感食堂活动群"))
        cfg = store.load()
        self.assertEqual(cfg["invite"]["keywords"]["灵感食堂"], "灵感食堂活动群")
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("删拉群 灵感食堂"))
        cfg = store.load()
        self.assertNotIn("灵感食堂", cfg["invite"]["keywords"])

    def test_welcome_management(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("设迎新文案 某群|你好 {name}"))
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("设迎新链接 某群|https://ncc.example.com"))
        cfg = store.load()
        self.assertEqual(cfg["welcome"]["某群"]["text"], "你好 {name}")
        self.assertEqual(cfg["welcome"]["某群"]["url"], "https://ncc.example.com")
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("关迎新 某群"))
        cfg = store.load()
        self.assertFalse(cfg["welcome"]["某群"]["enabled"])

    # ---------- welcome ----------

    def test_parse_new_member_patterns(self):
        cases = [
            ('你邀请"张三"加入了群聊', "张三"),
            ('"大曹"邀请"张三"加入了群聊', "张三"),
            ('"张三"通过扫描二维码加入群聊', "张三"),
            ("“张三”加入了群聊", "张三"),
            ("张三修改了群名", None),
        ]
        for content, expected in cases:
            self.assertEqual(welcome.parse_new_member(content), expected, content)

    def test_welcome_flow_with_card(self):
        chat = FakeChat("肥肉测试1")
        msg = FakeMsg('你邀请"新朋友"加入了群聊', mtype="system", attr="system")
        cfg = store.load()
        cfg["welcome"]["肥肉测试1"]["url"] = "https://ncc.example.com"
        store.save(cfg)
        handled = handle_system_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertIn("新朋友", chat.sent[0])
        self.assertEqual(self.bot.wx.url_cards, [("https://ncc.example.com", "肥肉测试1")])

    def test_welcome_disabled_group(self):
        chat = FakeChat("没配置的群")
        msg = FakeMsg('你邀请"新朋友"加入了群聊', attr="system")
        self.assertFalse(handle_system_message(self.bot, chat, msg))

    def test_welcome_skips_bot_itself(self):
        chat = FakeChat("肥肉测试1")
        msg = FakeMsg('你邀请"肥肉"加入了群聊', attr="system")
        self.assertFalse(handle_system_message(self.bot, chat, msg))

    # ---------- invite ----------

    def test_invite_not_triggered_in_group(self):
        # 拉群只在私聊生效：群里发关键词（含 @肥肉 前缀）都不处理
        chat = FakeChat("爱和一切肥肉测试群")
        self.assertFalse(handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明")))
        self.assertFalse(handle_friend_message(self.bot, chat, FakeMsg("@肥肉 测试拉群", sender="小明")))
        self.assertEqual(self.bot.wx.added_members, [])

    def test_invite_from_private(self):
        chat = FakeChat("小红", chat_type="friend")
        msg = FakeMsg("测试拉群", sender="小红")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.chatted, ["肥肉测试1"])
        self.assertEqual(self.bot.wx.added_members, [["小红"]])
        # 成功时静默拉群，不回话
        self.assertEqual(chat.sent, [])

    def test_invite_failure_reply(self):
        self.bot.wx.add_result = FakeWxResponse(False, "找不到该成员")
        chat = FakeChat("陌生人", chat_type="friend")
        msg = FakeMsg("测试拉群", sender="陌生人")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertIn("没成功", chat.sent[0])

    def test_invite_group_not_found_does_not_touch_members(self):
        """切群失败（ChatWith 静默返回 falsy）时绝不能去点"添加成员"：
        那会在残留的私聊窗口上操作，选不到人还可能【新建一个群】。"""
        self.bot.wx.fail_names = {"肥肉测试1"}
        self.bot.wx.current_chat = "小红"          # 窗口停在私聊上
        self.bot.wx.chat_types["小红"] = "friend"
        chat = FakeChat("小红", chat_type="friend")
        handled = handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小红"))
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.added_members, [])       # 没碰添加成员
        self.assertIn("没打开成功", chat.sent[0])              # 不再误报"不是好友"
        # 管理群收到接手提醒
        self.assertTrue(any(w == ADMIN_GROUP and "拉群没成功" in (m or "")
                            for w, m in self.bot.wx.sent_to))

    def test_invite_aborts_when_window_is_private_chat(self):
        """ChatWith 说切成功了，但 ChatInfo 显示当前是私聊 → 同样早退（防新建群）。"""
        self.bot.wx.chat_types["肥肉测试1"] = "friend"
        chat = FakeChat("小红", chat_type="friend")
        handled = handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小红"))
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.added_members, [])
        self.assertIn("没打开成功", chat.sent[0])

    def test_invite_falls_back_from_remark_to_group_name(self):
        """备注名搜不到时回退用群名重试（🐶备注丢了不该让整个关键词报废）。"""
        self._seed_registry_invites(
            {"大理": "NCC的大理朋友们3群"},
            {"NCC的大理朋友们3群": {"name": "NCC的大理朋友们3群",
                                   "remark": "NCC的大理朋友们3群🐶",
                                   "remark_applied": True}})
        self.bot.wx.fail_names = {"NCC的大理朋友们3群🐶"}
        chat = FakeChat("小红", chat_type="friend")
        handled = handle_friend_message(self.bot, chat, FakeMsg("大理", sender="小红"))
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.chatted[0], "NCC的大理朋友们3群🐶")   # 先试备注
        self.assertEqual(self.bot.wx.chatted[-1], "NCC的大理朋友们3群")    # 再回退群名
        self.assertEqual(self.bot.wx.added_members, [["小红"]])
        self.assertEqual(chat.sent, [])                                    # 成功不回话

    def test_invite_failure_notifies_admin_once_per_day(self):
        """同一个人反复试，管理群只被提醒一次（别刷屏）。"""
        self.bot.wx.add_result = FakeWxResponse(False, "找不到该成员")
        chat = FakeChat("小明", chat_type="friend")
        for _ in range(4):
            handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明"))
        notes = [m for w, m in self.bot.wx.sent_to if w == ADMIN_GROUP]
        self.assertEqual(len(notes), 1)

    def test_invite_failure_refund_is_capped(self):
        """退配额有上限：狂发关键词不能无限触发切群/选人的 UI 操作。"""
        self.bot.wx.add_result = FakeWxResponse(False, "找不到该成员")
        chat = FakeChat("小明", chat_type="friend")
        for _ in range(10):
            handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明"))
        # daily_limit 3 + 最多退 3 次 = 最多 6 次真正动手
        self.assertEqual(len(self.bot.wx.added_members), 6)
        self.assertIn("次数用完", chat.sent[-1])

    def test_invite_failure_refunds_quota(self):
        """失败不该吃掉当天额度：连失败 3 次后第 4 次仍能尝试。"""
        self.bot.wx.add_result = FakeWxResponse(False, "找不到该成员")
        chat = FakeChat("小明", chat_type="friend")
        for _ in range(3):
            handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明"))
        self.bot.wx.add_result = FakeWxResponse(True)
        handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明"))
        self.assertEqual(len(self.bot.wx.added_members), 4)
        self.assertNotIn("次数用完", "".join(chat.sent))

    def test_invite_daily_limit(self):
        chat = FakeChat("小明", chat_type="friend")
        for _ in range(3):
            handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明"))
        msg = FakeMsg("测试拉群", sender="小明")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertIn("次数用完", chat.sent[-1])
        self.assertEqual(len(self.bot.wx.added_members), 3)

    def test_non_keyword_not_handled(self):
        chat = FakeChat("小明", chat_type="friend")
        handled = handle_friend_message(self.bot, chat, FakeMsg("今天天气不错", sender="小明"))
        self.assertFalse(handled)

    # ---------- invite: Notion 同步来的关键词（registry.invite_keywords） ----------

    def _seed_registry_invites(self, keywords, groups=None):
        data = registry.load()
        data["invite_keywords"] = keywords
        if groups:
            data["groups"] = groups
        registry.save(data)

    def test_invite_keyword_from_registry(self):
        self._seed_registry_invites({"大理": "NCC的大理朋友们3群"})
        chat = FakeChat("小红", chat_type="friend")
        handled = handle_friend_message(self.bot, chat, FakeMsg("大理", sender="小红"))
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.chatted, ["NCC的大理朋友们3群"])
        self.assertEqual(self.bot.wx.added_members, [["小红"]])
        self.assertEqual(chat.sent, [])  # 成功静默

    def test_invite_registry_keyword_uses_remark_addressing(self):
        # 打过🐶备注的群按备注寻址（改群名也锁得住）
        self._seed_registry_invites(
            {"大理": "NCC的大理朋友们3群"},
            groups={"NCC的大理朋友们3群": {
                "name": "NCC的大理朋友们3群",
                "remark": "NCC的大理朋友们3群🐶",
                "remark_applied": True,
            }},
        )
        chat = FakeChat("小红", chat_type="friend")
        self.assertTrue(handle_friend_message(self.bot, chat, FakeMsg("大理", sender="小红")))
        self.assertEqual(self.bot.wx.chatted, ["NCC的大理朋友们3群🐶"])

    def test_manual_keyword_overrides_registry(self):
        self._seed_registry_invites({"测试拉群": "Notion指向的群"})
        # config.json 默认带 测试拉群 → 肥肉测试1，应覆盖 Notion 的同名关键词
        chat = FakeChat("小红", chat_type="friend")
        self.assertTrue(handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小红")))
        self.assertEqual(self.bot.wx.chatted, ["肥肉测试1"])

    def test_invite_list_shows_both_sources(self):
        self._seed_registry_invites({"大理": "NCC的大理朋友们3群"})
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("拉群列表"))
        out = self.admin_chat.sent[-1]
        self.assertIn("大理", out)
        self.assertIn("Notion", out)
        self.assertIn("测试拉群", out)



# ---------------------------------------------------------------- Notion 解析 / 改名迁移

def _title_prop(text):
    return {"title": [{"plain_text": text}]} if text is not None else {"title": []}


def _group_row(pid, title, forward=True, speak=True, url="", grouping_ids=()):
    return {
        "id": pid,
        "properties": {
            "群名": _title_prop(title),
            "允许转发": {"checkbox": forward},
            "允许发言": {"checkbox": speak},
            "迎新推送链接（填写后视为开启）": {"url": url},
            "转发群聊分组": {"relation": [{"id": i} for i in grouping_ids]},
        },
    }


def _grouping_row(pid, name, number=1, enabled=True):
    return {
        "id": pid,
        "properties": {
            "组名": _title_prop(name),
            "分组编号": {"number": number},
            "是否转发": {"checkbox": enabled},
        },
    }


class NotionParseTests(unittest.TestCase):
    """群名是寻址主键，解析时必须把人手滑打进 Notion 的空白洗掉。"""

    def test_strip_dog_variants(self):
        self.assertEqual(notion_sync._strip_dog("A群🐶"), ("A群", True))
        self.assertEqual(notion_sync._strip_dog("A群"), ("A群", False))
        self.assertEqual(notion_sync._strip_dog(""), ("", False))
        self.assertEqual(notion_sync._strip_dog(None), ("", False))

    def test_strip_dog_removes_surrounding_whitespace(self):
        # 2026-08-03 线上实到的形态：标题前导一个空格
        self.assertEqual(notion_sync._strip_dog(" NCC的朋友们17群🐶"), ("NCC的朋友们17群", True))
        self.assertEqual(notion_sync._strip_dog("A群 🐶"), ("A群", True))
        self.assertEqual(notion_sync._strip_dog("  A群🐶  "), ("A群", True))
        self.assertEqual(notion_sync._strip_dog("  A群  "), ("A群", False))

    def test_parse_notion_no_ghost_group_from_stray_space(self):
        """带空格的标题不能再解析出第二个"幽灵群"——它的寻址串在微信里不存在。"""
        rows = [_group_row("pid-17", " NCC的朋友们17群🐶")]
        _, groups = notion_sync.parse_notion(rows, [])
        self.assertEqual(list(groups), ["NCC的朋友们17群"])
        self.assertTrue(groups["NCC的朋友们17群"]["notion_marked"])

    def test_parse_notion_skips_blank_titles(self):
        rows = [_group_row("pid-a", ""), _group_row("pid-b", "   "), _group_row("pid-c", "真群🐶")]
        _, groups = notion_sync.parse_notion(rows, [])
        self.assertEqual(list(groups), ["真群"])

    def test_parse_notion_grouping_relation(self):
        grouping_rows = [_grouping_row("g1", "大理群", number=4)]
        rows = [_group_row("pid-1", "A群🐶", grouping_ids=["g1"])]
        groupings, groups = notion_sync.parse_notion(rows, grouping_rows)
        self.assertEqual(groupings["大理群"]["number"], 4)
        self.assertEqual(groups["A群"]["groupings"], ["大理群"])

    def test_parse_invites_strips_whitespace(self):
        group_rows = [_group_row("pid-1", " 目标群🐶 ")]
        invite_rows = [{"properties": {
            "关键词": _title_prop("  大理  "),
            "目标群": {"relation": [{"id": "pid-1"}]},
        }}]
        invites = notion_sync.parse_invites(invite_rows, group_rows)
        self.assertEqual(invites, {"大理": "目标群"})

    def test_update_title_dog_rejects_blank(self):
        with self.assertRaises(ValueError):
            notion_sync.update_title_dog("pid", "   ")


class RenameMigrationTests(unittest.TestCase):
    """群在 Notion 改名后，寻址必须继续用微信里那个真实存在的老备注。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ncc_rename_")
        self._orig_dir = registry.DATA_DIR
        self._orig_path = registry.REGISTRY_PATH
        registry.DATA_DIR = self.tmpdir
        registry.REGISTRY_PATH = os.path.join(self.tmpdir, "registry.json")

    def tearDown(self):
        registry.DATA_DIR = self._orig_dir
        registry.REGISTRY_PATH = self._orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, name, pid, remark=None, applied=True):
        registry.save({"groupings": {}, "invite_keywords": {}, "groups": {name: {
            "name": name, "remark": remark or (name + "🐶"), "remark_applied": applied,
            "notion_page_id": pid, "allow_forward": True, "allow_speak": True,
            "welcome_url": "", "groupings": ["朋友X群"], "status": "active",
            "last_seen": "2026-07-07T13:10:59",
        }}})

    def _incoming(self, name, pid, marked=True):
        return {name: {"notion_page_id": pid, "allow_forward": True, "allow_speak": True,
                       "welcome_url": "", "groupings": ["朋友X群"], "notion_marked": marked}}

    def test_rename_drops_old_key_and_keeps_wechat_remark(self):
        self._seed("老名", "pid-1")
        data = registry.upsert_from_notion({}, self._incoming("新名", "pid-1"))
        self.assertEqual(list(data["groups"]), ["新名"])          # 老 key 不再残留
        self.assertEqual(data["groups"]["新名"]["remark"], "老名🐶")  # 寻址仍指向真实会话
        self.assertEqual(registry.target(data["groups"]["新名"]), "老名🐶")
        self.assertEqual(data["renamed_last_sync"], [{"from": "老名", "to": "新名"}])

    def test_rename_carries_last_seen(self):
        self._seed("老名", "pid-1")
        data = registry.upsert_from_notion({}, self._incoming("新名", "pid-1"))
        self.assertEqual(data["groups"]["新名"]["last_seen"], "2026-07-07T13:10:59")

    def test_rename_without_remark_follows_new_name(self):
        """没打过备注的群按群名寻址，改名后就该跟新名走。"""
        self._seed("老名", "pid-1", applied=False)
        data = registry.upsert_from_notion({}, self._incoming("新名", "pid-1", marked=False))
        self.assertEqual(data["groups"]["新名"]["remark"], "新名🐶")
        self.assertEqual(registry.target(data["groups"]["新名"]), "新名")

    def test_no_migration_when_old_key_still_present_in_notion(self):
        """老 key 自己也是本次 Notion 里的有效群时，绝不能被当成改名删掉。"""
        self._seed("A群", "pid-1")
        incoming = {}
        incoming.update(self._incoming("A群", "pid-1"))
        incoming.update(self._incoming("B群", "pid-2"))
        data = registry.upsert_from_notion({}, incoming)
        self.assertEqual(sorted(data["groups"]), ["A群", "B群"])
        self.assertEqual(data["renamed_last_sync"], [])

    def test_ghost_entry_removed_when_real_key_already_exists(self):
        """线上实到的形态：真群和带空格的幽灵条目并存，pid 相同。
        Notion 标题洗干净后，这一条必须被清掉——不能靠"新 key 不存在"那条路。"""
        registry.save({"groupings": {}, "invite_keywords": {}, "groups": {
            "NCC的朋友们17群": {"name": "NCC的朋友们17群", "remark": "NCC的朋友们17群🐶",
                                "remark_applied": True, "notion_page_id": "pid-17",
                                "allow_forward": True, "groupings": [], "status": "active"},
            " NCC的朋友们17群": {"name": " NCC的朋友们17群", "remark": " NCC的朋友们17群🐶",
                                 "remark_applied": True, "notion_page_id": "pid-17",
                                 "allow_forward": True, "groupings": ["朋友X群"], "status": "active"},
        }})
        data = registry.upsert_from_notion({}, self._incoming("NCC的朋友们17群", "pid-17"))
        self.assertEqual(list(data["groups"]), ["NCC的朋友们17群"])
        self.assertNotIn(" NCC的朋友们17群🐶", registry.all_forward_targets(data))

    def test_orphan_with_unknown_pid_is_kept(self):
        """Notion 行被删的孤儿不在本次结果里，但 pid 也没被谁认领——不该顺手删掉。"""
        registry.save({"groupings": {}, "invite_keywords": {}, "groups": {
            "已删群": {"name": "已删群", "remark": "已删群🐶", "remark_applied": True,
                       "notion_page_id": "pid-gone", "allow_forward": False,
                       "groupings": [], "status": "active"},
        }})
        data = registry.upsert_from_notion({}, self._incoming("别的群", "pid-1"))
        self.assertIn("已删群", data["groups"])

    def test_plain_update_preserves_remark_applied(self):
        self._seed("A群", "pid-1")
        data = registry.upsert_from_notion({}, self._incoming("A群", "pid-1", marked=False))
        self.assertTrue(data["groups"]["A群"]["remark_applied"])
        self.assertEqual(data["renamed_last_sync"], [])

    def test_notion_dog_marks_applied_without_local_record(self):
        self._seed("A群", "pid-1", applied=False)
        data = registry.upsert_from_notion({}, self._incoming("A群", "pid-1", marked=True))
        self.assertTrue(data["groups"]["A群"]["remark_applied"])



# ---------------------------------------------------------------- 备注名实核对

class RemarkAuditTests(unittest.TestCase):
    """查"A 群的🐶备注被打到 B 群头上"。可达性检查对这类错误完全隐形：
    错打时 ChatWith 是成功的，只不过切到的是被错打的那个群。"""

    KNOWN = {"肥肉测试1", "泰国清迈旅居交流1群", "爱和未来"}

    def _info(self, chat_name, remark="", chat_type="group"):
        return {"chat_name": chat_name, "remark": remark, "chat_type": chat_type}

    def test_misapplied_remark_detected(self):
        """线上实到的形态：肥肉测试1🐶 打到了泰国清迈旅居交流1群头上。"""
        info = self._info("泰国清迈旅居交流1群", remark="肥肉测试1🐶")
        verdict, detail = audit.classify("肥肉测试1", info, self.KNOWN)
        self.assertEqual(verdict, audit.MISAPPLIED)
        self.assertIn("肥肉测试1🐶", detail)
        self.assertIn("泰国清迈旅居交流1群", detail)

    def test_correct_remark_is_ok(self):
        info = self._info("肥肉测试1", remark="肥肉测试1🐶")
        self.assertEqual(audit.classify("肥肉测试1", info, self.KNOWN)[0], audit.OK)

    def test_unknown_chat_name_reads_as_rename_not_misapply(self):
        """切到的群不在册 → 多半只是改了名，不该误报成错打。"""
        info = self._info("大理新群名", remark="肥肉测试1🐶")
        self.assertEqual(audit.classify("肥肉测试1", info, self.KNOWN)[0], audit.RENAMED)

    def test_remark_landed_on_friend(self):
        info = self._info("松爸", remark="肥肉测试1🐶", chat_type="friend")
        verdict, detail = audit.classify("肥肉测试1", info, self.KNOWN)
        self.assertEqual(verdict, audit.NOT_GROUP)
        self.assertIn("松爸", detail)

    def test_missing_remark(self):
        verdict, _ = audit.classify("肥肉测试1", None, self.KNOWN)
        self.assertEqual(verdict, audit.MISSING)

    def test_inconclusive_when_real_name_unreadable(self):
        """wxautox 把备注串塞进 chat_name、remark 又是空的时候，判不了真名。"""
        info = self._info("肥肉测试1🐶", remark="")
        self.assertEqual(audit.classify("肥肉测试1", info, self.KNOWN)[0], audit.INCONCLUSIVE)

    def test_chat_name_equals_remark_but_remark_present_is_ok(self):
        info = self._info("肥肉测试1🐶", remark="肥肉测试1🐶")
        self.assertEqual(audit.classify("肥肉测试1", info, self.KNOWN)[0], audit.OK)

    def test_summarize_flags_misapplied_first_and_warns(self):
        results = [
            ("爱和未来", audit.OK, ""),
            ("肥肉测试1", audit.MISAPPLIED, "备注「肥肉测试1🐶」实际打在了群「泰国清迈旅居交流1群」头上"),
            ("某群", audit.RENAMED, "改名了"),
        ]
        out = audit.summarize(results)
        self.assertIn("名实相符 1 个", out)
        self.assertIn("🔴", out)
        self.assertLess(out.index("🔴"), out.index("🟡"))   # 高危排在前面
        self.assertIn("追加", out)                          # 提示清备注的坑

    def test_summarize_all_ok_has_no_warning(self):
        out = audit.summarize([("爱和未来", audit.OK, "")])
        self.assertIn("名实相符 1 个", out)
        self.assertNotIn("🔴", out)


class SelectGroupsTests(unittest.TestCase):
    DATA = {
        "groupings": {"肥肉自测": {}},
        "groups": {
            "肥肉测试1": {"groupings": ["肥肉自测"]},
            "爱和未来": {"groupings": ["肥肉自测"]},
            "清迈群": {"groupings": ["合作社群群"]},
        },
    }

    def test_all(self):
        self.assertEqual(sorted(n for n, _ in forward._select_groups(self.DATA, "全部")),
                         sorted(["爱和未来", "肥肉测试1", "清迈群"]))

    def test_by_grouping(self):
        self.assertEqual(sorted(n for n, _ in forward._select_groups(self.DATA, "肥肉自测")),
                         sorted(["爱和未来", "肥肉测试1"]))

    def test_unknown_scope_returns_none(self):
        self.assertIsNone(forward._select_groups(self.DATA, "不存在的分组"))



# ---------------------------------------------------------------- 打备注前后的两道校验

class FakeRemarkWx:
    """按"当前窗口"返回 ChatInfo；SetGroupRemark 按 wxautox 的真实行为【追加】。"""

    def __init__(self, world, cur=None):
        self.world = world      # {会话键: {"chat_name":.., "remark":.., "chat_type":..}}
        self.cur = cur
        self.remark_calls = []

    def ChatWith(self, who=None, exact=False):
        # 模糊搜索：先按群名精确找，再按【子串】命中备注——微信就是这个行为，
        # 也正是错打的入口：搜「肥肉测试1」会命中备注「肥肉测试1🐶」的那个群
        for key, info in self.world.items():
            if info.get("chat_name") == who:
                self.cur = key
                return True
        for key, info in self.world.items():
            if who and who in (info.get("remark") or ""):
                self.cur = key
                return True
        return False

    def ChatInfo(self):
        return self.world.get(self.cur)

    def SetGroupRemark(self, remark):
        self.remark_calls.append((self.cur, remark))
        info = self.world[self.cur]
        info["remark"] = (info.get("remark") or "") + remark   # 追加，不是替换
        return True


class ConfirmBeforeRemarkTests(unittest.TestCase):
    """打备注前的复核：群名和备注【两边都要对得上】。"""

    def test_rejects_misapplied_window(self):
        """核心回归：备注已被错打到清迈群，再给肥肉测试1打备注时不能放行。

        旧判据 `want in (name.rstrip(DOG), rmk.rstrip(DOG))` 在这里会返回 True——
        rmk 剥掉🐶恰好等于目标群名，等于让错误替自己背书，于是又追加打一次。"""
        wx = FakeRemarkWx({"清迈": {"chat_type": "group",
                                    "chat_name": "泰国清迈旅居交流1群",
                                    "remark": "肥肉测试1🐶"}}, cur="清迈")
        ok, why = remark.confirm_group_window(wx, "肥肉测试1")
        self.assertFalse(ok)
        self.assertIn("错打", why)
        self.assertIn("泰国清迈旅居交流1群", why)

    def test_accepts_clean_target_group(self):
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试1", "remark": ""}}, cur="a")
        self.assertTrue(remark.confirm_group_window(wx, "肥肉测试1")[0])

    def test_accepts_group_already_having_exact_target_remark(self):
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试1",
                                 "remark": "肥肉测试1🐶"}}, cur="a")
        self.assertTrue(remark.confirm_group_window(wx, "肥肉测试1")[0])

    def test_rejects_group_with_other_remark(self):
        """群名对上了，但它已经有别的备注——再打会追加成垃圾。"""
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试1",
                                 "remark": "别的备注🐶"}}, cur="a")
        ok, why = remark.confirm_group_window(wx, "肥肉测试1")
        self.assertFalse(ok)
        self.assertIn("追加", why)

    def test_rejects_similar_group_name(self):
        """模糊搜索命中名字相近的另一个群。"""
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试12", "remark": ""}}, cur="a")
        self.assertFalse(remark.confirm_group_window(wx, "肥肉测试1")[0])

    def test_rejects_unreadable_real_name(self):
        """读不到真实群名就不动手——没有它无从判断切到的是谁。"""
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "", "remark": "肥肉测试1🐶"}}, cur="a")
        self.assertFalse(remark.confirm_group_window(wx, "肥肉测试1")[0])

    def test_rejects_non_group(self):
        wx = FakeRemarkWx({"a": {"chat_type": "friend", "chat_name": "松爸", "remark": ""}}, cur="a")
        self.assertFalse(remark.confirm_group_window(wx, "肥肉测试1")[0])


class VerifyAfterRemarkTests(unittest.TestCase):
    """打完之后的回读复核：SetGroupRemark 的返回值不可信（None 都算成功）。"""

    def test_passes_when_remark_landed(self):
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试1",
                                 "remark": "肥肉测试1🐶"}}, cur="a")
        self.assertTrue(remark.verify_remark(wx, "肥肉测试1", "肥肉测试1🐶")[0])

    def test_fails_when_remark_appended(self):
        """追加坑的现场：回读发现备注是拼接出来的，不能记进登记表。"""
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试1",
                                 "remark": "旧备注🐶肥肉测试1🐶"}}, cur="a")
        ok, why = remark.verify_remark(wx, "肥肉测试1", "肥肉测试1🐶")
        self.assertFalse(ok)
        self.assertIn("回读备注", why)

    def test_fails_when_landed_on_another_group(self):
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "泰国清迈旅居交流1群",
                                 "remark": "肥肉测试1🐶"}}, cur="a")
        self.assertFalse(remark.verify_remark(wx, "肥肉测试1", "肥肉测试1🐶")[0])


class ApplyRemarkEndToEndTests(unittest.TestCase):
    """整条 apply_remark 链路：错打场景下必须【一次都不写】。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ncc_apply_")
        self._d, self._p = registry.DATA_DIR, registry.REGISTRY_PATH
        registry.DATA_DIR = self.tmpdir
        registry.REGISTRY_PATH = os.path.join(self.tmpdir, "registry.json")
        registry.save({"groupings": {}, "invite_keywords": {}, "groups": {
            "肥肉测试1": {"name": "肥肉测试1", "remark": "肥肉测试1🐶",
                          "remark_applied": False, "notion_page_id": "p1"},
        }})

    def tearDown(self):
        registry.DATA_DIR, registry.REGISTRY_PATH = self._d, self._p
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_does_not_write_when_remark_already_misapplied_elsewhere(self):
        """真群不在会话里、备注却挂在清迈群上：ChatWith 会命中备注切到清迈群。
        必须拒绝，且【绝不能】调用 SetGroupRemark。"""
        wx = FakeRemarkWx({"清迈": {"chat_type": "group",
                                    "chat_name": "泰国清迈旅居交流1群",
                                    "remark": "肥肉测试1🐶"}})
        ok, info = remark.apply_remark(wx, "肥肉测试1")
        self.assertFalse(ok)
        self.assertEqual(wx.remark_calls, [])          # 一次都没写
        self.assertIn("错打", info)
        self.assertFalse(registry.load()["groups"]["肥肉测试1"]["remark_applied"])

    def test_writes_and_marks_on_clean_group(self):
        wx = FakeRemarkWx({"a": {"chat_type": "group", "chat_name": "肥肉测试1", "remark": ""}})
        ok, info = remark.apply_remark(wx, "肥肉测试1")
        self.assertTrue(ok)
        self.assertEqual(wx.remark_calls, [("a", "肥肉测试1🐶")])
        self.assertTrue(registry.load()["groups"]["肥肉测试1"]["remark_applied"])


class FixRemarkPlanTests(unittest.TestCase):
    """「修备注」的判定：期望值就地取自当前窗口的真实群名。"""

    def test_already_correct(self):
        self.assertEqual(audit.plan_remark("大理群", "大理群🐶"),
                         (audit.FIX_OK, "大理群🐶"))

    def test_empty_remark_can_apply(self):
        self.assertEqual(audit.plan_remark("大理群", ""),
                         (audit.FIX_APPLY, "大理群🐶"))

    def test_other_remark_is_conflict(self):
        """已有别的备注只能人工清——SetGroupRemark 是追加，硬打会变成垃圾。"""
        v, d = audit.plan_remark("泰国清迈旅居交流1群", "肥肉测试1🐶")
        self.assertEqual(v, audit.FIX_CONFLICT)
        self.assertIn("泰国清迈旅居交流1群🐶", d)

    def test_appended_garbage_is_conflict(self):
        v, _ = audit.plan_remark("肥肉测试1", "肥肉测试1🐶肥肉测试1🐶")
        self.assertEqual(v, audit.FIX_CONFLICT)

    def test_no_real_name_skips(self):
        self.assertEqual(audit.plan_remark("", "随便")[0], audit.FIX_SKIP)

    def test_expectation_never_comes_from_outside(self):
        """核心回归：期望备注只由真实群名决定，跟"我们以为它是谁"无关。
        所以切歪了顶多给另一个群打上它自己的正确备注，不会复现 A 打到 B 的错打。"""
        _, want = audit.plan_remark("泰国清迈旅居交流1群", "")
        self.assertEqual(want, "泰国清迈旅居交流1群🐶")


class ExtractGroupNamesTests(unittest.TestCase):
    """GetAllRecentGroups 的返回结构没有文档（编译发行），抽名字要能兜住各种形状。"""

    def test_tuples(self):
        self.assertEqual(audit.extract_group_names([("大理群", 3), ("黄山群", 5)]),
                         ["大理群", "黄山群"])

    def test_plain_strings(self):
        self.assertEqual(audit.extract_group_names(["大理群"]), ["大理群"])

    def test_dicts_and_objects(self):
        class Obj:
            name = "对象群"
        self.assertEqual(audit.extract_group_names([{"name": "字典群"}, Obj()]),
                         ["字典群", "对象群"])

    def test_dedup_and_strip(self):
        self.assertEqual(audit.extract_group_names([" 大理群 ", "大理群", ""]), ["大理群"])

    def test_empty(self):
        self.assertEqual(audit.extract_group_names(None), [])

    def test_describe_raw_does_not_crash(self):
        self.assertIn("共 2 项", audit.describe_raw([("a", 1), ("b", 2)]))


class SummarizeFixTests(unittest.TestCase):
    def test_conflicts_are_spelled_out_for_manual_work(self):
        out = audit.summarize_fix([
            ("A群", audit.FIX_OK, "A群🐶"),
            ("B群", audit.FIX_APPLY, "B群🐶"),
            ("C群", audit.FIX_CONFLICT, "现备注「野的🐶」，应为「C群🐶」"),
            ("D群", audit.FIX_FAILED, "打完复核不通过"),
            ("E群", audit.FIX_SKIP, "切不过去"),
        ])
        self.assertIn("共 5 个群", out)
        self.assertIn("C群", out)
        self.assertIn("手动清空", out)
        self.assertIn("D群", out)

    def test_dry_run_lists_todo(self):
        out = audit.summarize_fix([("B群", audit.FIX_APPLY, "B群🐶")], dry=True)
        self.assertIn("预览", out)
        self.assertIn("B群 → B群🐶", out)


class TaskRunnerTests(unittest.TestCase):
    """后台任务触发器：外部写一行指令，bot 进程内执行，回复落到结果文件。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ncc_task_")
        self._req, self._res = task_runner.REQUEST_PATH, task_runner.RESULT_PATH
        task_runner.REQUEST_PATH = os.path.join(self.tmpdir, "task_request.txt")
        task_runner.RESULT_PATH = os.path.join(self.tmpdir, "task_result.txt")

    def tearDown(self):
        task_runner.REQUEST_PATH, task_runner.RESULT_PATH = self._req, self._res
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, text, age=0.0):
        with open(task_runner.REQUEST_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        if age:
            old = time.time() - age
            os.utime(task_runner.REQUEST_PATH, (old, old))

    def test_request_is_consumed_and_deleted(self):
        """先删再执行：崩了也不会开机重放。"""
        self._write("扫群")
        text, age = task_runner._read_request()
        self.assertEqual(text, "扫群")
        self.assertLess(age, 5)
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))

    def test_no_request_is_noop(self):
        self.assertEqual(task_runner._read_request(), (None, 0.0))

    def test_stale_request_is_dropped(self):
        """bot 没在跑时攒下的请求不能隔几小时诈尸。"""
        self._write("修备注 全部", age=task_runner.STALE_SECONDS + 60)
        task_runner.tick(object())
        self.assertFalse(os.path.exists(task_runner.RESULT_PATH))

    def test_file_sink_collects_replies(self):
        sink = task_runner.FileSink(task_runner.RESULT_PATH)
        sink.SendMsg(msg="第一条")
        sink.SendMsg(msg="第二条")
        with open(task_runner.RESULT_PATH, encoding="utf-8") as f:
            out = f.read()
        self.assertIn("第一条", out)
        self.assertIn("第二条", out)

    def test_unknown_command_is_reported_not_swallowed(self):
        task_runner._run(FakeBot(), "这不是指令")
        with open(task_runner.RESULT_PATH, encoding="utf-8") as f:
            out = f.read()
        self.assertIn("不认识的指令", out)
        self.assertIn("任务结束", out)

    def test_runs_a_real_direct_command(self):
        """跑一条不碰微信的直接指令，验证 forward 的指令层原样复用。"""
        task_runner._run(FakeBot(), "分组列表")
        with open(task_runner.RESULT_PATH, encoding="utf-8") as f:
            out = f.read()
        self.assertIn("=== 后台任务「分组列表」开始", out)
        self.assertIn("任务结束", out)
        self.assertNotIn("不认识的指令", out)


if __name__ == "__main__":
    unittest.main()
