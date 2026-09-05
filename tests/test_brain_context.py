# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from brain.gateway import context

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


if __name__ == "__main__":
    unittest.main()
