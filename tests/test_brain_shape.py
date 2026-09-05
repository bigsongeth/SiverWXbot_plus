# -*- coding: utf-8 -*-
"""回复形状纯函数：预算 / 剥 Markdown / 剥收尾套话 / 反口头禅 / 截断。"""
from __future__ import annotations
import unittest
from brain.gateway import shape
from brain.gateway.config import DEFAULTS


class BudgetTest(unittest.TestCase):
    def test_short_incoming_hits_floor(self):
        self.assertEqual(shape.budget("滴滴", True, DEFAULTS), 40)

    def test_scales_with_incoming(self):
        # 30 + 2.5*20 = 80
        self.assertEqual(shape.budget("一" * 20, True, DEFAULTS), 80)

    def test_group_and_private_caps(self):
        long = "字" * 200
        self.assertEqual(shape.budget(long, True, DEFAULTS), 150)
        self.assertEqual(shape.budget(long, False, DEFAULTS), 220)

    def test_text_len_ignores_whitespace(self):
        self.assertEqual(shape.text_len(" a b\n c "), 3)

    def test_text_len_ignores_urls(self):
        self.assertEqual(shape.text_len("蛙喔牛蛙 https://food.bigsong.site/p/B0IUHDMPBE 紫苏味"), 7)

    def test_max_bubbles(self):
        self.assertEqual(shape.max_bubbles(True, DEFAULTS), 2)
        self.assertEqual(shape.max_bubbles(False, DEFAULTS), 3)


class StripTest(unittest.TestCase):
    def test_strip_markdown_reuses_reply_shape(self):
        out = shape.strip_markdown("**加粗** 和\n# 标题\n---\n正文")
        self.assertNotIn("**", out); self.assertNotIn("#", out); self.assertNotIn("---", out)
        self.assertIn("加粗", out); self.assertIn("正文", out)

    def test_strip_closing_drops_last_bubble(self):
        out = shape.strip_closing(["大理还在运营。", "需要我把地址发你吗？"])
        self.assertEqual(out, ["大理还在运营。"])

    def test_strip_closing_keeps_real_question(self):
        out = shape.strip_closing(["你说的是黄山还是大理？"])
        self.assertEqual(out, ["你说的是黄山还是大理？"])

    def test_strip_closing_single_bubble_drops_last_sentence(self):
        out = shape.strip_closing(["大理还在运营，黑多岛也在。还要我继续介绍吗？"])
        self.assertEqual(out, ["大理还在运营，黑多岛也在。"])

    def test_strip_closing_leaves_non_question_alone(self):
        out = shape.strip_closing(["需要我的话随时叫。"])
        self.assertEqual(out, ["需要我的话随时叫。"])


class RepeatTest(unittest.TestCase):
    def test_same_opener_is_repeat(self):
        r = shape.repeats("我刚从键盘上趴起来，看到你说滴滴", ["我刚从键盘上趴起来，刷到消息"])
        self.assertEqual(r, "我刚从键盘上趴起来，刷到消息")

    def test_high_ngram_overlap_is_repeat(self):
        r = shape.repeats("大理据点还在运营中哦", ["嗯，大理据点还在运营中哦"])
        self.assertIsNotNone(r)

    def test_different_reply_not_repeat(self):
        self.assertIsNone(shape.repeats("黄山这周有活动", ["大理据点还在运营"]))


class TruncateTest(unittest.TestCase):
    def test_truncate_drops_overflow_bubbles(self):
        out = shape.truncate_to(["一" * 30, "二" * 30], 40)
        self.assertEqual(out, ["一" * 30])

    def test_truncate_cuts_single_bubble_at_sentence(self):
        out = shape.truncate_to(["第一句。第二句很长很长很长。第三句。"], 8)
        self.assertEqual(out, ["第一句。"])

    def test_truncate_hard_cut_when_no_sentence_end(self):
        out = shape.truncate_to(["一" * 50], 10)
        self.assertEqual(out, ["一" * 10])


if __name__ == "__main__":
    unittest.main()
