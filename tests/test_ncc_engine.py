# -*- coding: utf-8 -*-
"""Phase 1 引擎单测：registry / forward 菜单 / discovery / remark / notion 解析。
全部用假对象，不碰微信、不碰真 Notion。"""
from __future__ import annotations

import itertools
import os
import shutil
import tempfile
import time
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


class FakeWxResponse:
    """wxautox 的 WxResponse：失败时 falsy，错误文案在 ["message"] 里。
    （本文件以前漏了这个类，转发失败分支实际抛的是 NameError，
    等于一直在测异常路径而不是真实返回值路径。）"""
    def __init__(self, ok=True, message=""):
        self.ok = ok
        self.message = message

    def __bool__(self):
        return self.ok

    def __getitem__(self, key):
        return {"message": self.message}.get(key)


_UID = itertools.count(1)


class FakeMsg:
    def __init__(self, content="", mtype="text", attr="friend", sender="大松"):
        self.content = content
        self.type = mtype
        self.attr = attr
        self.sender = sender
        self.id = f"m{next(_UID)}"   # 真实 wxautox Message 自带 id，去重靠它
        self.forwarded = []      # 记录被转发到的所有群（forward 收列表）
        self.forward_ok = True   # False 模拟整条转不了（视频号等）
        self.gone_set = set()    # 这些群"无结果"（被踢/解散）
        self.raise_set = set()   # 这些群转发时抛异常（超时/UI 抽风这类说不清的错）
        self.hang_set = set()    # 这些群转发时【卡住不返回】（wxautox 搜不到目标的真实行为）
        self.hang_secs = 30.0    # 卡多久

    def forward(self, target, **kw):
        # 单目标模型：一次转 1 个群（字符串）；容错也接受列表
        groups = target if isinstance(target, list) else [target]
        if any(g in self.hang_set for g in groups):
            time.sleep(self.hang_secs)
            return None
        if any(g in self.raise_set for g in groups):
            raise RuntimeError("timeout: 等待发送窗口超时")
        if not self.forward_ok:
            return FakeWxResponse(False, "转发失败")
        if any(g in self.gone_set for g in groups):
            return FakeWxResponse(False, "无结果")   # 该群没了 → 该次失败
        self.forwarded.extend(groups)
        return None

    def roll_into_view(self):
        pass


ZERO_DELAY = {"group_min": 0, "group_max": 0, "msg_min": 0, "msg_max": 0,
              "batch_every": 10, "batch_min": 0, "batch_max": 0, "max_retries": 1}


def _prompt():
    """一条 COLLECT_PROMPT 自己消息，作为收集起点边界。"""
    return FakeMsg(content="🤖 请发送需要转发的内容，一个一个来", mtype="text",
                   attr="self", sender="self")


def build_timeline(bot, *content_msgs, operator="大松"):
    """给源群时间线放上 [收集起点, 内容消息...]，模拟用户发的内容。"""
    for m in content_msgs:
        m.attr = "friend"; m.sender = operator
    bot.wx.timeline = [_prompt(), *content_msgs]


class FakeWx:
    nickname = "🐶肥肉"

    def __init__(self):
        self.chatted = []
        self.remarks = {}     # 群名 -> 备注（模拟“追加”行为验证幂等）
        self.sent = []
        self.timeline = []    # 源群消息时间线（_gather_content 现读这个）
        self.visible = None   # 不为 None 时表示"此刻可见"只有这些（模拟滚出可见区）
        self.chat_types = {}  # 会话名 -> chat_type，缺省当群聊

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)
        return None           # 真实 wxautox 成功时可能返回 None

    def ChatInfo(self):
        """打备注/加成员前的窗口复核要用（真实 WeChat 有这个方法）。"""
        last = self.chatted[-1] if self.chatted else None
        if last is None:
            return {}
        # 真机上 ChatInfo 只有 {'chat_type','chat_name','group_member_count'}：
        # 没有 remark 字段，而且群一旦有备注，chat_name 显示的就是备注本身。
        return {"chat_name": self.remarks.get(last) or last,
                "chat_type": self.chat_types.get(last, "group")}

    def GetSubWindow(self, nickname):
        return None

    def GetHistoryMessage(self, n=100, callback=None, **kw):
        msgs = list(self.timeline)
        if callback:
            for m in msgs:
                callback(m)   # 触发停止回调（测试里不真截断）
        return msgs

    def GetAllMessage(self):
        return list(self.timeline if self.visible is None else self.visible)

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
        forward.GATHER_SETTLE = 0    # 测试里不等 UI
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
        # 源群里用户发的内容（含视频号 other 类型）
        m1 = FakeMsg("[视频号]大曹", mtype="other")
        m2 = FakeMsg("正文", mtype="text")
        build_timeline(self.bot, m1, m2)
        # ncc → 主菜单 → 1 进入转发收集
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        self.assertIn("一个一个来", self.admin.sent[-1])
        self.assertEqual(forward._get_state("大松")["state"], forward.S_FWD_COLLECT)
        # 发 1 → 从源群【现读】2 条 + 选分组菜单
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        self.assertIn("已收集 2 条消息", self.admin.sent[-1])
        self.assertIn("4 👈 大理群", self.admin.sent[-1])   # 大理群 分组编号=4
        self.assertEqual(forward._get_state("大松")["state"], forward.S_FWD_CHOOSE)
        # 选“4” → 入队 + 状态清除
        handle_friend_message(self.bot, self.admin, FakeMsg("4"))
        self.assertIsNone(forward._get_state("大松"))
        self.assertIn("开始转发 2 条消息到 2 个群", self.admin.sent[-1])
        # 执行队列：worker 再从源群现读 → 转发（一群一群）
        self._drain()
        self.assertEqual(sorted(m1.forwarded), ["大理A群", "大理B群"])
        self.assertEqual(sorted(m2.forwarded), ["大理A群", "大理B群"])
        self.assertIn("转发完成", " ".join(m for _, m in self.bot.wx.sent))

    def test_forward_all_groups_option(self):
        seed_registry()
        m = FakeMsg("正文", mtype="text")
        build_timeline(self.bot, m)
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))     # 现读 1 条，进入选分组
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))     # 1 = 所有群聊
        self.assertIn("开始转发 1 条消息到 2 个群", self.admin.sent[-1])
        self._drain()
        report = " ".join(m for _, m in self.bot.wx.sent)
        self.assertIn("所有群聊", report)
        self.assertIn("2 个群", report)
        self.assertEqual(sorted(m.forwarded), ["大理A群", "大理B群"])

    def test_forward_multiselect(self):
        groupings = {"大理群": {"number": 4, "forward_enabled": True},
                     "黄山群": {"number": 6, "forward_enabled": True}}
        groups = {
            "大A": {"notion_page_id": "1", "allow_forward": True, "allow_speak": True, "welcome_url": "", "groupings": ["大理群"]},
            "黄A": {"notion_page_id": "2", "allow_forward": True, "allow_speak": True, "welcome_url": "", "groupings": ["黄山群"]},
        }
        registry.upsert_from_notion(groupings, groups)
        m = FakeMsg("正文", mtype="text")
        build_timeline(self.bot, m)
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("4+6"))   # 多选
        self.assertIn("开始转发 1 条消息到 2 个群", self.admin.sent[-1])
        self._drain()
        self.assertEqual(sorted(m.forwarded), ["大A", "黄A"])

    def test_gather_excludes_commands_and_other_senders(self):
        # 现读只取本 operator 的、非数字指令的内容
        m1 = FakeMsg("真内容", mtype="text")
        cmd = FakeMsg("1", mtype="text")               # 指令数字，应排除
        other = FakeMsg("别人发的", mtype="text")       # 他人消息，应排除
        m1.attr = cmd.attr = other.attr = "friend"
        m1.sender = cmd.sender = "大松"; other.sender = "路人"
        self.bot.wx.timeline = [_prompt(), other, m1, cmd]
        got = forward._gather_content(self.bot, ADMIN, "大松")
        self.assertEqual([g.content for g in got], ["真内容"])

    def test_exit_zero(self):
        seed_registry()
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("0"))
        self.assertIsNone(forward._get_state("大松"))
        self.assertIn("已退出", self.admin.sent[-1])

    def test_collect_proceed_without_content(self):
        seed_registry()
        self.bot.wx.timeline = [_prompt()]   # 只有起点，无内容
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))
        handle_friend_message(self.bot, self.admin, FakeMsg("1"))   # 现读为空
        self.assertIn("还未读到任何要转发的消息", self.admin.sent[-1])
        self.assertEqual(forward._get_state("大松")["state"], forward.S_FWD_COLLECT)

    def test_menu_2_gives_panel_url(self):
        """菜单 2 从「同步 Notion」改成了「管理面板地址」（去 Notion 化）。"""
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        handle_friend_message(self.bot, self.admin, FakeMsg("2"))
        self.assertIn("/ncc_community", self.admin.sent[-1])

    def test_menu_no_longer_offers_sync(self):
        handle_friend_message(self.bot, self.admin, FakeMsg("ncc"))
        self.assertNotIn("同步 Notion", self.admin.sent[-1])

    def test_bare_number_without_state_ignored(self):
        seed_registry()  # 无状态
        handled = handle_friend_message(self.bot, self.admin, FakeMsg("3"))
        self.assertFalse(handled)   # 不劫持普通群聊里的数字

    def test_deliver_many_groups_single_target(self):
        # 12 个群 → 一个群一个群转（单目标），一条消息 forward 12 次覆盖全部
        m = FakeMsg("素材", mtype="text")
        build_timeline(self.bot, m)
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": [f"群{i}" for i in range(12)], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["ok"], 12)
        self.assertEqual(stat["gone"], [])
        self.assertEqual(sorted(m.forwarded), sorted(f"群{i}" for i in range(12)))

    def test_deliver_gone_group_marked_and_skipped(self):
        # 群1 已没了(无结果)：单群转发返回无结果→只把群1判不可达，其余照发
        m = FakeMsg("素材", mtype="text"); m.gone_set = {"群1"}
        build_timeline(self.bot, m)
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": ["群0", "群1", "群2"], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["gone"], ["群1"])
        self.assertEqual(stat["ok"], 2)
        self.assertEqual(sorted(m.forwarded), ["群0", "群2"])
        self.assertIn("搜不到", " ".join(mm for _, mm in self.bot.wx.sent))

    # ---------- 寻址候选 / 卡死兜底（2026-08-04 全量转发第一个群就卡死那次）----------

    def test_address_candidates_order_follows_display_name(self):
        # 顺序要按【微信里的显示名】猜：wxautox 得在搜索结果里找显示名一致的项才勾得中，
        # 打了🐶的群显示的就是备注，拿群名去搜是"搜得到但勾不中"（8/10 实测）
        g = {"name": "甲群", "remark": "甲群🐶", "remark_applied": True}
        self.assertEqual(registry.address_candidates(g), ["甲群🐶", "甲群"])
        # 没打备注的群，显示名就是群名
        g2 = {"name": "乙群", "remark": "乙群🐶", "remark_applied": False}
        self.assertEqual(registry.address_candidates(g2), ["乙群", "乙群🐶"])
        # 实测命中过的永远排最前，第二轮起不用再猜
        g["addressing_hit"] = "甲群"
        self.assertEqual(registry.address_candidates(g), ["甲群", "甲群🐶"])

    def test_forward_falls_back_to_remark_candidate(self):
        # 首选串搜不到 → 自动换备选串，并把实测结果记进 addressing_hit
        seed_registry()
        m = FakeMsg("素材", mtype="text")
        m.gone_set = {"大理A群"}                       # 群名搜不到
        build_timeline(self.bot, m)
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": [{"name": "大理A群", "cands": ["大理A群", "大理A群🐶"]}],
                "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["ok"], 1)
        self.assertEqual(m.forwarded, ["大理A群🐶"])
        self.assertEqual(stat["gone"], [])
        self.assertEqual(registry.load()["groups"]["大理A群"]["addressing_hit"], "大理A群🐶")

    def test_forward_call_timeout_skips_group_and_continues(self):
        # wxautox 搜不到目标时会卡着不返回：限时判失败、继续下一个群，不许拖垮整轮
        m = FakeMsg("素材", mtype="text")
        m.hang_set = {"群1"}
        m.hang_secs = 1.0
        build_timeline(self.bot, m)
        d = dict(ZERO_DELAY); d["call_timeout"] = 0.2
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": ["群0", "群1", "群2"], "label": "x", "delay": d}
        stat = forward._deliver(task)
        self.assertEqual(sorted(m.forwarded), ["群0", "群2"])   # 卡住那个跳过，其余照发
        self.assertEqual(stat["gone"], ["群1"])                 # 搜不到 → 标记不可达
        self.assertEqual(stat["ok"], 2)

    def test_forward_stuck_aborts_the_round(self):
        # ESC 也没能让它退出 = 全局 UI 锁没释放，再转下去只会一路卡死 → 立刻收手
        orig_grace = forward.STUCK_GRACE
        forward.STUCK_GRACE = 0.2
        try:
            m = FakeMsg("素材", mtype="text")
            m.hang_set = {"群1"}
            m.hang_secs = 30.0
            build_timeline(self.bot, m)
            d = dict(ZERO_DELAY); d["call_timeout"] = 0.2
            task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                    "targets": ["群0", "群1", "群2"], "label": "x", "delay": d}
            stat = forward._deliver(task)
        finally:
            forward.STUCK_GRACE = orig_grace
        self.assertEqual(m.forwarded, ["群0"])      # 群2 没再试
        self.assertEqual(stat["gone"], [])          # 不是群的问题，别冤枉它
        self.assertIn("卡死", " ".join(mm for _, mm in self.bot.wx.sent))

    def test_gate_keepalive_extends_hold(self):
        from plugins.ncc_community import wxlock
        orig = wxlock._MAX_HOLD
        wxlock._MAX_HOLD = 0.3
        try:
            wxlock.set_forwarding(True)
            time.sleep(0.4)
            self.assertFalse(wxlock.is_forwarding())     # 久无续期 = 转发线程死了，放行
            wxlock.set_forwarding(True)
            time.sleep(0.2)
            wxlock.keepalive()
            time.sleep(0.2)
            self.assertTrue(wxlock.is_forwarding())      # 续过期 → 闸门还举着
        finally:
            wxlock.set_forwarding(False)
            wxlock._MAX_HOLD = orig

    def test_deliver_two_images_both_sent(self):
        # 两张图片签名一模一样（wxautox 里图片 content 固定是「图片」）：
        # 按签名去重会吞掉第二张，必须按 id 去重 + 按序号定位
        img1 = FakeMsg("图片", mtype="image")
        img2 = FakeMsg("图片", mtype="image")
        build_timeline(self.bot, img1, img2)
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": ["群0", "群1"], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["ok"], 4)                        # 2 条 × 2 群
        self.assertEqual(sorted(img1.forwarded), ["群0", "群1"])
        self.assertEqual(sorted(img2.forwarded), ["群0", "群1"])
        self.assertIn("2 条", " ".join(m for _, m in self.bot.wx.sent))

    def test_deliver_second_image_out_of_view_is_skipped_not_duplicated(self):
        # 第二张图滚出可见区 → 宁可漏发并汇报，也不能把第一张再发一遍
        img1 = FakeMsg("图片", mtype="image")
        img2 = FakeMsg("图片", mtype="image")
        build_timeline(self.bot, img1, img2)
        self.bot.wx.visible = [_prompt(), img1]
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": ["群0", "群1"], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(sorted(img1.forwarded), ["群0", "群1"])
        self.assertEqual(img2.forwarded, [])
        self.assertEqual(stat["gone"], [])                     # 不是群的问题，别标记
        # 第 2 条一个群都没成 → 归到"整条转发失败"，汇报里看得见，不会悄悄少发
        self.assertIn("第2条 整条转发失败", " ".join(m for _, m in self.bot.wx.sent))

    def test_deliver_unknown_error_does_not_mark_group_unreachable(self):
        # 超时/UI 抽风这类说不清的错：只汇报，不能写 registry 把群禁掉
        seed_registry()
        m = FakeMsg("素材", mtype="text")
        m.raise_set = {"大理B群"}
        build_timeline(self.bot, m)
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": ["大理A群", "大理B群"], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["gone"], [])
        self.assertEqual(stat["ok"], 1)
        self.assertTrue(registry.load()["groups"]["大理B群"]["allow_forward"])
        self.assertIn("其它失败", " ".join(mm for _, mm in self.bot.wx.sent))

    def test_deliver_unforwardable_does_not_blame_groups(self):
        # 整条都转不了（视频号）→ 判定是消息问题，不冤枉任何群（gone 为空）
        vid = FakeMsg("[视频号]", mtype="other"); vid.forward_ok = False
        build_timeline(self.bot, vid)
        task = {"bot": self.bot, "admin": ADMIN, "operator": "大松",
                "targets": [f"群{i}" for i in range(6)], "label": "x", "delay": dict(ZERO_DELAY)}
        stat = forward._deliver(task)
        self.assertEqual(stat["gone"], [])          # 没有群被标记不可达
        self.assertEqual(stat["ok"], 0)
        self.assertIn("整条转发失败", " ".join(m for _, m in self.bot.wx.sent))

    def test_sync_command_retired_but_answers(self):
        """「同步」已下线，但仍要回一句人话 —— 直接装不认识的话，
        习惯性发「同步」的人会以为机器人挂了。且绝不能再打 Notion。"""
        called = {}
        orig = notion_sync.pull
        notion_sync.pull = lambda: called.setdefault("v", True)
        try:
            handled = handle_friend_message(self.bot, self.admin, FakeMsg("同步"))
        finally:
            notion_sync.pull = orig
        self.assertTrue(handled)
        self.assertFalse(called, "「同步」不该再调 Notion")
        self.assertIn("下线", self.admin.sent[-1])
        self.assertIn("/ncc_community", self.admin.sent[-1])

    def test_backend_command_gives_panel_url(self):
        handle_friend_message(self.bot, self.admin, FakeMsg("后台"))
        self.assertIn("/ncc_community", self.admin.sent[-1])

    def test_grouping_list_and_pending(self):
        seed_registry()
        registry.add_pending("某新群")
        handle_friend_message(self.bot, self.admin, FakeMsg("分组列表"))
        self.assertIn("大理群", self.admin.sent[-1])
        handle_friend_message(self.bot, self.admin, FakeMsg("待归类"))
        self.assertIn("某新群", self.admin.sent[-1])

    # ---------- remark ----------

    def test_apply_remark_refuses_when_window_not_the_group(self):
        """切群静默失败/切歪了就不能打备注——wxautox 的备注是追加且清不掉的。"""
        seed_registry()
        self.bot.wx.chat_types["大理A群"] = "friend"   # 窗口其实是个私聊
        ok, info = remark.apply_remark(self.bot.wx, "大理A群")
        self.assertFalse(ok)
        self.assertEqual(self.bot.wx.remarks, {})
        self.assertIn("切到该群失败", info)

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
        # 去 Notion 化后不再往 Notion 推待归类行：本地 add_pending 就够了，
        # 面板「待归类」页直接能看到并归类。这里守住"绝不再打 Notion"。
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
        self.assertFalse(pushed, "发现新群不该再写 Notion")
        # 管理群收到提醒，且指向面板
        alerts = [m for _, m in self.bot.wx.sent if "发现新群" in (m or "")]
        self.assertTrue(alerts)
        self.assertIn("/ncc_community", alerts[-1])

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

    def test_parse_invites(self):
        group_rows = [
            {"id": "row1", "properties": {"群名": {"title": [{"plain_text": "NCC大理一家人🐶"}]}}},
            {"id": "row2", "properties": {"群名": {"title": [{"plain_text": "黄山总部"}]}}},
        ]
        invite_rows = [
            # 标题列名带尾随空格（真实表就是「让对方回复 」），按类型解析不受影响
            {"properties": {"让对方回复 ": {"title": [{"plain_text": " 大理 "}]},
                            "拉入群聊": {"relation": [{"id": "row1"}]}}},
            {"properties": {"让对方回复 ": {"title": [{"plain_text": "黄山"}]},
                            "拉入群聊": {"relation": [{"id": "row2"}]}}},
            # 目标群不在群聊列表 → 跳过
            {"properties": {"让对方回复 ": {"title": [{"plain_text": "火星"}]},
                            "拉入群聊": {"relation": [{"id": "nowhere"}]}}},
            # 缺关键词 → 跳过
            {"properties": {"让对方回复 ": {"title": []},
                            "拉入群聊": {"relation": [{"id": "row2"}]}}},
        ]
        invites = notion_sync.parse_invites(invite_rows, group_rows)
        self.assertEqual(invites, {"大理": "NCC大理一家人", "黄山": "黄山总部"})

    def test_upsert_invite_keywords(self):
        # 带 invite_keywords 时写入；下次不带（None）时保留原值
        registry.upsert_from_notion({}, {}, {"大理": "群A"})
        self.assertEqual(registry.load()["invite_keywords"], {"大理": "群A"})
        registry.upsert_from_notion({}, {})
        self.assertEqual(registry.load()["invite_keywords"], {"大理": "群A"})


if __name__ == "__main__":
    unittest.main()
