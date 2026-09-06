# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from unittest.mock import patch
from brain.gateway import context
from plugins.context_guard import guard

IDX = {"hz-food-map": {"groups": ["共建杭州美食地图"]}, "ncc-community": {"scope": "all"}}


class SkillsTest(unittest.TestCase):
    def test_group_specific_plus_all(self):
        self.assertEqual(context.match_skills(IDX, "共建杭州美食地图", True), ["hz-food-map", "ncc-community"])

    def test_other_group_only_all(self):
        self.assertEqual(context.match_skills(IDX, "肥肉测试1🐶", True), ["ncc-community"])


class PrimeTest(unittest.TestCase):
    def test_drops_system_and_checkin(self):
        items = [
            {"time": "2026/08/03 19:00:00", "type": "time", "attr": "system", "sender": "system", "content": "19:00"},
            {"time": "2026/08/03 19:01:00", "type": "text", "attr": "friend", "sender": "K", "content": "签到！"},
            {"time": "2026/08/03 19:01:05", "type": "text", "attr": "self", "sender": "肥肉", "content": "兑换码 BTC-MF86-GEU6-BEM5，去 key.bigsong.site 兑换"},
            {"time": "2026/08/03 19:01:30", "type": "text", "attr": "self", "sender": "肥肉", "content": "在忙，我稍后回复您"},
            {"time": "2026/08/03 19:02:00", "type": "text", "attr": "friend", "sender": "K", "content": "大理还开着吗"},
        ]
        out = context.filter_prime(items, 20)
        self.assertEqual([x["content"] for x in out], ["大理还开着吗"])  # 系统时间戳/签到/兑换码/兜底文案全部不进

    def test_is_checkin_text(self):
        self.assertTrue(context.is_checkin_text("签到"))
        self.assertTrue(context.is_checkin_text(" 打卡~ "))
        self.assertFalse(context.is_checkin_text("签到功能是怎么做的"))

    def test_keeps_last_n(self):
        items = [{"time": "t", "type": "text", "attr": "friend", "sender": "a", "content": str(i)} for i in range(30)]
        self.assertEqual([x["content"] for x in context.filter_prime(items, 3)], ["27", "28", "29"])

    def test_prime_keeps_quote_replies(self):
        items = [
            {"time": "1", "type": "text", "attr": "friend", "sender": "K", "content": "滴滴"},
            {"time": "2", "type": "quote", "attr": "self", "sender": "self", "content": "汪"},
        ]
        self.assertEqual([x["content"] for x in context.filter_prime(items, 20)], ["滴滴", "汪"])

    def test_group_prime_not_wiped_by_round_connivance(self):
        # 群里两条兜底文案之间夹着十几条别人的发言：私聊式"整轮连坐"会把它们全摘掉
        items = [{"time": "0", "type": "text", "attr": "self", "sender": "self", "content": "在忙，我稍后回复您"}]
        items += [{"time": str(i), "type": "text", "attr": "friend", "sender": f"u{i}", "content": f"闲聊{i}"} for i in range(1, 15)]
        items += [{"time": "15", "type": "text", "attr": "self", "sender": "self", "content": "在忙，我稍后回复您"},
                  {"time": "16", "type": "text", "attr": "friend", "sender": "K", "content": "@肥肉 在吗"}]
        out = context.filter_prime(items, 20, is_group=True)
        self.assertEqual(len(out), 15)
        self.assertNotIn("在忙，我稍后回复您", [x["content"] for x in out])

    def test_private_prime_falls_back_when_connivance_empties_it(self):
        items = [
            {"time": "1", "type": "text", "attr": "friend", "sender": "K", "content": "问题一"},
            {"time": "2", "type": "text", "attr": "self", "sender": "self", "content": "在忙，我稍后回复您"},
        ]
        out = context.filter_prime(items, 20, is_group=False)
        self.assertEqual([x["content"] for x in out], ["问题一"])

    def test_prime_drops_fallback_even_if_context_guard_disabled(self):
        items = [
            {"time": "2026/09/05 10:00:00", "type": "text", "attr": "friend", "sender": "K", "content": "第一个问题"},
            {"time": "2026/09/05 10:00:05", "type": "text", "attr": "self", "sender": "肥肉", "content": "在忙，我稍后回复您"},
            {"time": "2026/09/05 10:00:10", "type": "text", "attr": "self", "sender": "肥肉", "content": "[NO_REPLY]"},
            {"time": "2026/09/05 10:00:15", "type": "text", "attr": "self", "sender": "肥肉", "content": "我这边没法联网哦"},
            {"time": "2026/09/05 10:00:20", "type": "text", "attr": "friend", "sender": "K", "content": "第二个问题"},
        ]
        with patch.object(guard, "_load_config", return_value={"enabled": False}):
            out = context.filter_prime(items, 20)
        self.assertEqual([x["content"] for x in out], ["第一个问题", "第二个问题"])


class MessageTest(unittest.TestCase):
    def test_prefix_line_group(self):
        m = context.build_user_message("共建杭州美食地图", True, "松爸", "吃啥", "2026-09-05 14:02", ["hz-food-map"])
        self.assertEqual(m, "[群聊:共建杭州美食地图 | 发言人:松爸 | 2026-09-05 14:02 | 相关技能:hz-food-map]\n松爸: 吃啥")

    def test_private_with_prime(self):
        prime = [{"time": "2026/09/05 13:00:00", "attr": "friend", "sender": "K", "content": "你好"},
                 {"time": "2026/09/05 13:00:10", "attr": "self", "sender": "肥肉", "content": "汪"}]
        m = context.build_user_message("K", False, "K", "在吗", "2026-09-05 14:02", [], prime=prime)
        self.assertTrue(m.startswith("[私聊:K | 发言人:K | 2026-09-05 14:02 | 相关技能:无]\n"))
        self.assertIn("以下是此前的聊天记录，只供了解背景，不要逐条回复：\n[2026/09/05 13:00:00] K: 你好\n[2026/09/05 13:00:10] 你(肥肉): 汪\n---\n", m)
        self.assertTrue(m.endswith("K: 在吗"))


class RenderTest(unittest.TestCase):
    """小程序卡片/位置消息进历史时剥掉微信拼出来的前缀，标出类型，模型不用猜哪段是店名。"""
    def test_dianping_card(self):
        item = {"type": "miniapp", "content": "小程序大众点评美食电影运动旅游门票潮汕菜大排档·卤味砂锅粥|快餐简餐"}
        self.assertEqual(context.render_content(item), "[大众点评卡片] 潮汕菜大排档·卤味砂锅粥|快餐简餐")

    def test_meituan_card(self):
        item = {"type": "miniapp", "content": "小程序美团外卖丨外卖美食奶茶咖啡水果凡老头米线砂锅"}
        self.assertEqual(context.render_content(item), "[美团外卖卡片] 凡老头米线砂锅")

    def test_unknown_miniapp(self):
        self.assertEqual(context.render_content({"type": "miniapp", "content": "小程序某某小程序标题"}), "[小程序卡片] 某某小程序标题")

    def test_location(self):
        item = {"type": "location", "content": "位置舟村·砂锅焗海鲜(城北店)浙江省杭州市余杭区良渚街道金昌路2136号"}
        self.assertEqual(context.render_content(item), "[位置] 舟村·砂锅焗海鲜(城北店)浙江省杭州市余杭区良渚街道金昌路2136号")

    def test_text_untouched(self):
        self.assertEqual(context.render_content({"type": "text", "content": "好吃+1"}), "好吃+1")

    def test_prime_keeps_cards_and_locations(self):
        items = [{"time": "1", "type": "miniapp", "attr": "friend", "sender": "鹅", "content": "小程序大众点评美食电影运动旅游门票潮汕菜大排档"},
                 {"time": "2", "type": "location", "attr": "friend", "sender": "鹅", "content": "位置舟村金昌路2136号"},
                 {"time": "3", "type": "image", "attr": "friend", "sender": "鹅", "content": "[图片]"}]
        self.assertEqual([x["type"] for x in context.filter_prime(items, 20, is_group=True)], ["miniapp", "location"])


class DeltaTest(unittest.TestCase):
    """每轮都要带历史：会话已预热后，把上次之后群里新出现的消息挑出来（用户 2026-09-06 拍板）。"""
    def setUp(self):
        self.old = [{"time": "1", "type": "text", "attr": "friend", "sender": "K", "content": "早"}]
        self.card = {"time": "2", "type": "miniapp", "attr": "friend", "sender": "鹅", "content": "小程序大众点评美食电影运动旅游门票潮汕菜大排档"}

    def test_only_unseen_items(self):
        seen = {context.fingerprint(x) for x in self.old}
        out = context.select_new(self.old + [self.card], seen, is_group=True)
        self.assertEqual(out, [self.card])

    def test_excludes_current_message_and_junk(self):
        cur = {"time": "3", "type": "text", "attr": "friend", "sender": "松爸", "content": "@🐶肥肉 收一下上面那家"}
        junk = {"time": "4", "type": "time", "attr": "system", "sender": "system", "content": "13:14"}
        out = context.select_new([self.card, cur, junk], set(), is_group=True, exclude=("松爸", "收一下上面那家"))
        self.assertEqual(out, [self.card])

    def test_caps_count(self):
        items = [{"time": str(i), "type": "text", "attr": "friend", "sender": "a", "content": str(i)} for i in range(10)]
        self.assertEqual([x["content"] for x in context.select_new(items, set(), True, count=3)], ["7", "8", "9"])

    def test_message_with_delta_label(self):
        m = context.build_user_message("共建杭州美食地图", True, "松爸", "收一下", "2026-09-06 13:20", [], prime=[self.card],
                                       prime_label="以下是上一轮之后群里新出现的消息")
        self.assertIn("以下是上一轮之后群里新出现的消息\n[2] 鹅: [大众点评卡片] 潮汕菜大排档\n---\n", m)


if __name__ == "__main__":
    unittest.main()
