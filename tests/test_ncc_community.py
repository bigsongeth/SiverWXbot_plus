# -*- coding: utf-8 -*-
"""ncc_community 插件单元测试（不依赖微信环境，全部用假对象）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from plugins.ncc_community import store, forward, invite, welcome
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

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)

    def AddGroupMembers(self, members=None, **kwargs):
        self.added_members.append(list(members))
        return self.add_result

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
        forward._SESSIONS.clear()
        invite._QUOTA.clear()
        self.bot = FakeBot()
        self.admin_chat = FakeChat(ADMIN_GROUP)

    def tearDown(self):
        store.DATA_DIR = self._orig_data_dir
        store.CONFIG_PATH = self._orig_config_path
        store._cache = None
        store._cache_mtime = None
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

    def test_group_list_command(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("分组列表"))
        self.assertIn("测试组", self.admin_chat.sent[0])
        self.assertIn("肥肉测试1", self.admin_chat.sent[0])

    def test_forward_unknown_group(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("转发 不存在的组"))
        self.assertIn("没有「不存在的组」", self.admin_chat.sent[0])
        self.assertIsNone(forward._get_session("大松"))

    def test_forward_full_flow(self):
        # 进入收集模式
        handled = handle_friend_message(self.bot, self.admin_chat, FakeMsg("转发 测试组"))
        self.assertTrue(handled)
        self.assertIsNotNone(forward._get_session("大松"))
        # 发素材（图片消息）→ 被转发到两个群
        material = FakeMsg("[图片]", mtype="image")
        handled = handle_friend_message(self.bot, self.admin_chat, material)
        self.assertTrue(handled)
        self.assertEqual(material.forwarded_to, [["肥肉测试1", "爱和一切肥肉测试群"]])
        self.assertIn("2 个群", self.admin_chat.sent[-1])
        # 结束
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("结束"))
        self.assertIsNone(forward._get_session("大松"))
        self.assertIn("成功 2", self.admin_chat.sent[-1])

    def test_forward_failure_reported(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("转发 测试组"))
        material = FakeMsg("hello", mtype="text")
        material.forward_ok = False
        handle_friend_message(self.bot, self.admin_chat, material)
        self.assertIn("失败", self.admin_chat.sent[-1])

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

    def test_chunking(self):
        cfg = store.load()
        cfg["forward"]["groups"]["大组"] = [f"群{i}" for i in range(10)]
        cfg["forward"]["chunk_size"] = 4
        store.save(cfg)
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("转发 大组"))
        material = FakeMsg("素材", mtype="text")
        handle_friend_message(self.bot, self.admin_chat, material)
        self.assertEqual([len(part) for part in material.forwarded_to], [4, 4, 2])

    # ---------- forward: 分组维护 ----------

    def test_group_management(self):
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("新建分组 在地群"))
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("加群 在地群|黄山在地"))
        cfg = store.load()
        self.assertEqual(cfg["forward"]["groups"]["在地群"], ["黄山在地"])
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("删群 在地群|黄山在地"))
        cfg = store.load()
        self.assertEqual(cfg["forward"]["groups"]["在地群"], [])
        handle_friend_message(self.bot, self.admin_chat, FakeMsg("删除分组 在地群"))
        cfg = store.load()
        self.assertNotIn("在地群", cfg["forward"]["groups"])

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

    def test_invite_from_group(self):
        chat = FakeChat("爱和一切肥肉测试群")
        msg = FakeMsg("测试拉群", sender="小明")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.chatted, ["肥肉测试1"])
        self.assertEqual(self.bot.wx.added_members, [["小明"]])
        self.assertIn("已邀请", chat.sent[0])

    def test_invite_from_private(self):
        chat = FakeChat("小红", chat_type="friend")
        msg = FakeMsg("测试拉群", sender="小红")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.added_members, [["小红"]])

    def test_invite_with_at_prefix(self):
        chat = FakeChat("爱和一切肥肉测试群")
        msg = FakeMsg("@肥肉 测试拉群", sender="小明")
        self.assertTrue(handle_friend_message(self.bot, chat, msg))

    def test_invite_failure_reply(self):
        self.bot.wx.add_result = FakeWxResponse(False, "找不到该成员")
        chat = FakeChat("爱和一切肥肉测试群")
        msg = FakeMsg("测试拉群", sender="陌生人")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertIn("没成功", chat.sent[0])

    def test_invite_daily_limit(self):
        chat = FakeChat("爱和一切肥肉测试群")
        for _ in range(3):
            handle_friend_message(self.bot, chat, FakeMsg("测试拉群", sender="小明"))
        msg = FakeMsg("测试拉群", sender="小明")
        handled = handle_friend_message(self.bot, chat, msg)
        self.assertTrue(handled)
        self.assertIn("次数用完", chat.sent[-1])
        self.assertEqual(len(self.bot.wx.added_members), 3)

    def test_non_keyword_not_handled(self):
        chat = FakeChat("爱和一切肥肉测试群")
        handled = handle_friend_message(self.bot, chat, FakeMsg("今天天气不错", sender="小明"))
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
