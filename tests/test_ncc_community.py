# -*- coding: utf-8 -*-
"""ncc_community 插件单元测试（不依赖微信环境，全部用假对象）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from plugins.ncc_community import store, forward, invite, registry, welcome
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


if __name__ == "__main__":
    unittest.main()
