# -*- coding: utf-8 -*-
"""回复形状纯函数：预算 / 剥 Markdown / 剥收尾套话 / 反口头禅 / 截断。"""
from __future__ import annotations
import unittest
from brain.gateway import shape
from brain.gateway.config import DEFAULTS


KW = ("ncc", "大理", "据点", "价格", "多少钱", "报名", "rag", "部署")


def _hist(*pairs, sender="小明"):
    """构造历史：pairs 是 (content, attr) 或纯字符串（默认 friend）。"""
    out = []
    for p in pairs:
        c, attr = p if isinstance(p, tuple) else (p, "friend")
        out.append({"time": "10:00", "sender": sender, "content": c, "type": "text", "attr": attr})
    return out


class ErrandAxisTest(unittest.TestCase):
    """维度 A：办事 vs 闲聊。"""

    def test_keyword_hit_is_errand(self):
        self.assertTrue(shape.is_errand("大理那边还开着吗", KW))
        self.assertTrue(shape.is_errand("RAG 怎么调", KW))

    def test_keyword_is_case_insensitive(self):
        self.assertTrue(shape.is_errand("NCC 是啥", KW))
        self.assertTrue(shape.is_errand("ncc 是啥", KW))

    def test_question_shape_is_errand_without_keyword(self):
        # 长尾技术问题不在词表里，靠问句形态兜住
        self.assertTrue(shape.is_errand("这个玩意儿在哪下载", KW))
        self.assertTrue(shape.is_errand("能不能帮我看看", KW))

    def test_plain_chat_is_not_errand(self):
        self.assertFalse(shape.is_errand("哈哈哈笑死我了", KW))
        self.assertFalse(shape.is_errand("今天真冷", KW))

    def test_short_greeting_is_not_errand(self):
        """光凭一个问号不该当成办事：「在吗？」曾拿到 86 字预算，实际只用 18。"""
        for t in ("在吗？", "你好？", "在不在", "肥肉？"):
            self.assertFalse(shape.is_errand(t, KW), t)

    def test_short_message_with_keyword_is_still_errand(self):
        # 短，但有实词 —— 照样是办事
        self.assertTrue(shape.is_errand("大理呢？", KW))

    def test_longer_question_survives_greeting_shortcut(self):
        self.assertTrue(shape.is_errand("现在签到好了吗？", KW))

    def test_empty_text(self):
        self.assertFalse(shape.is_errand("", KW))

    def test_errand_hits_reports_words(self):
        self.assertEqual(set(shape.errand_hits("大理的价格是多少", KW)), {"大理", "价格"})

    def test_missing_keyword_file_returns_empty(self):
        self.assertEqual(shape.load_errand_keywords("/nonexistent/xx.txt"), ())

    def test_real_keyword_file_loads(self):
        kws = shape.load_errand_keywords()
        self.assertIn("ncc", kws)          # 表里存的是小写
        self.assertIn("据点", kws)
        self.assertNotIn("", kws)
        self.assertFalse(any(w.startswith("#") for w in kws))


class DepthAxisTest(unittest.TestCase):
    """维度 B：对方想不想深入。只看对方的措辞和行为，不问模型。"""

    def test_explicit_depth_words(self):
        for t in ("展开说说", "能详细讲讲吗", "举个例子", "多说点", "什么原理", "细说"):
            self.assertTrue(shape.wants_depth(t, None, "小明", KW), t)

    def test_plain_question_is_not_deep(self):
        self.assertFalse(shape.wants_depth("大理还开着吗", None, "小明", KW))

    def test_why_is_not_a_depth_signal(self):
        """「为什么/为啥」刻意不算深度信号 —— 日常问句里太常见。

        回放生产日志时抓到的：「为啥吃寿司郎要排队呀？」曾被判成想深入、拿到群聊顶格 150，
        而它实际只值 37 字。问原因归「办事」，深度只认明确的「多说点」类措辞。
        """
        self.assertFalse(shape.wants_depth("为啥吃寿司郎要排队呀？", None, "小明", KW))
        self.assertFalse(shape.wants_depth("为什么会这样", None, "小明", KW))
        self.assertTrue(shape.is_errand("为啥吃寿司郎要排队呀？", KW))   # 但它是「办事」

    def test_followup_shares_keyword(self):
        h = _hist("大理还能去吗")
        self.assertTrue(shape.wants_depth("大理什么价格", h, "小明", KW))

    def test_followup_shares_bigrams(self):
        h = _hist("那个模型跑起来卡不卡")
        self.assertTrue(shape.is_followup("那个模型跑起来要多久", h, "小明", ()))

    def test_unrelated_history_is_not_followup(self):
        h = _hist("今天天气真好")
        self.assertFalse(shape.is_followup("大理什么价格", h, "小明", KW))

    def test_ignores_self_messages(self):
        # 机器人自己说过「大理」不算对方在追问
        h = _hist(("大理还开着", "self"))
        self.assertFalse(shape.is_followup("大理什么价格", h, "小明", KW))

    def test_ignores_other_senders(self):
        h = _hist("大理还能去吗", sender="别人")
        self.assertFalse(shape.is_followup("大理什么价格", h, "小明", KW))

    def test_excludes_current_message_itself(self):
        # 插件传的历史里往往含当前这条；自己跟自己比必然 100% 重合
        h = _hist("大理什么价格")
        self.assertFalse(shape.is_followup("大理什么价格", h, "小明", KW))

    def test_no_history(self):
        self.assertFalse(shape.is_followup("大理什么价格", None, "小明", KW))
        self.assertFalse(shape.is_followup("大理什么价格", [], "小明", KW))

    def test_malformed_history_items_are_skipped(self):
        self.assertFalse(shape.is_followup("大理什么价格", ["不是字典", None], "小明", KW))


class BudgetMatrixTest(unittest.TestCase):
    """四个格子 + 夹逼 + legacy 回退。数值同 spec §6.2。"""

    def _b(self, text, is_group=False, history=None):
        return shape.budget(text, is_group, DEFAULTS, history, "小明", KW)

    def test_chat_normal_hits_floor(self):
        # (45 + 0.5*2) = 46 → 夹到下限 55
        self.assertEqual(self._b("滴滴"), 55)

    def test_errand_normal(self):
        # 「NCC 据点在哪？」8 字：(85 + 0.5*8) * 1.0 = 89
        self.assertEqual(self._b("NCC据点在哪？"), 89)

    def test_chat_deep(self):
        # 「展开说说」4 字：(45 + 0.5*4) * 1.7 = 79.9 → 79
        self.assertEqual(self._b("展开说说"), 79)

    def test_errand_followup_uses_weak_multiplier(self):
        # 追问是弱信号：(85 + 0.5*10) * 1.25 = 112.5
        h = _hist("大理还能去吗")
        self.assertEqual(self._b("那大理具体什么价格？", history=h), 112)

    def test_explicit_depth_beats_followup(self):
        # 明说要展开是强信号：(85 + 0.5*9) * 1.7 = 152.15
        h = _hist("大理还能去吗")
        self.assertEqual(self._b("大理的价格详细讲讲", history=h), 152)

    def test_long_errand_question_counts_as_explicit(self):
        """认真打一长段来问正事 = 投入度信号。

        回放抓到的：松爸用 111 字问「基于这个剧本我们能做什么」，实际答了 218 字，
        而措辞里没有任何「详细讲讲」—— 光靠追问的弱加成会被裁掉。
        """
        long_q = "如果说这个币会让大家想到当时发币吸走流动性" * 3 + "，我们现在可以做什么？"
        self.assertEqual(shape.depth_level(long_q, None, "小明", KW, long_input=True), "explicit")

    def test_long_chat_does_not_count_as_explicit(self):
        # 闲聊灌一大段不该跟着放宽（闲聊格实用 max 只有 52 字）
        long_chat = "哈哈哈今天真的笑死我了这个也太好玩了吧" * 4
        self.assertEqual(shape.depth_level(long_chat, None, "小明", KW, long_input=True), "")

    def test_depth_levels(self):
        h = _hist("大理还能去吗")
        self.assertEqual(shape.depth_level("详细讲讲", None, "小明", KW), "explicit")
        self.assertEqual(shape.depth_level("大理什么价格", h, "小明", KW), "followup")
        self.assertEqual(shape.depth_level("今天天气不错", None, "小明", KW), "")

    def test_followup_no_longer_maxes_out_short_price_question(self):
        """回放抓到的：连续问币价被判追问，×1.7 会顶到群聊上限 150，而实际只值 51 字。"""
        h = _hist("现在比特币多少钱一个")
        self.assertLess(self._b("以太坊现在什么价", is_group=True, history=h), 130)

    def test_long_chat_no_longer_maxes_out(self):
        # 旧公式下 111 字闲聊顶格 220，新公式 (45 + 55.5) = 100 —— 长输入规则只对办事生效
        self.assertEqual(self._b("哈" * 111), 100)

    def test_long_errand_reaches_cap(self):
        # 111 字的办事提问：(85 + 55.5) * 1.7 = 238.85 → 私聊上限 220，装得下那条 218 字的回复
        self.assertEqual(self._b("大理" + "这个问题想请教一下" * 11), 220)

    def test_caps(self):
        # 办事 + 想深入 + 长输入：(85 + 0.5*206) * 1.7 = 319.6，两边都该被上限夹住
        long_errand = "详细讲讲大理" + "字" * 200
        self.assertEqual(self._b(long_errand, is_group=True), 150)
        self.assertEqual(self._b(long_errand, is_group=False), 220)

    def test_medium_errand_without_depth_stays_modest(self):
        # 40 字的办事提问，没到长输入阈值(60)也没追问：(85 + 0.5*40) * 1.0 = 105
        self.assertEqual(self._b("大理" + "字" * 38, is_group=False), 105)

    def test_floor_applies(self):
        self.assertEqual(self._b(""), 55)

    def test_detail_reports_verdict(self):
        val, why = shape.budget_detail("大理的价格", False, DEFAULTS, None, "小明", KW)
        self.assertEqual(why["mode"], "two_axis")
        self.assertTrue(why["errand"])
        self.assertFalse(why["deep"])
        self.assertIn("大理", why["hits"])
        self.assertEqual(why["in_len"], 5)

    def test_legacy_mode_restores_old_formula(self):
        cfg = {**DEFAULTS, "budget": {**DEFAULTS["budget"], "mode": "legacy", "min": 40}}
        self.assertEqual(shape.budget("滴滴", True, cfg), 40)          # 30 + 2.5*2 = 35 → 下限 40
        self.assertEqual(shape.budget("一" * 20, True, cfg), 80)       # 30 + 2.5*20
        self.assertEqual(shape.budget("字" * 200, True, cfg), 150)     # 撞群上限
        self.assertEqual(shape.budget("字" * 200, False, cfg), 220)

    def test_urls_do_not_eat_budget(self):
        a = self._b("这家店不错")
        b = self._b("这家店不错 https://food.bigsong.site/p/B0J2BAMJAK")
        self.assertEqual(a, b)

    def test_default_signature_still_works(self):
        # 老调用方式（不传 history/sender/keywords）不能炸
        self.assertIsInstance(shape.budget("大理在哪", False, DEFAULTS), int)


class FullwidthTest(unittest.TestCase):
    def test_converts_in_chinese_context(self):
        self.assertEqual(shape.to_fullwidth("在呢,有事?"), "在呢，有事？")

    def test_converts_when_only_right_side_is_chinese(self):
        self.assertEqual(shape.to_fullwidth("GPT-6:能力强"), "GPT-6：能力强")

    def test_parens_around_chinese(self):
        self.assertEqual(shape.to_fullwidth("能力惊(3D视频)厉害"), "能力惊（3D视频）厉害")

    def test_leaves_pure_english(self):
        self.assertEqual(shape.to_fullwidth("Hi, how are you?"), "Hi, how are you?")

    def test_leaves_numbers(self):
        self.assertEqual(shape.to_fullwidth("共 3,000 元"), "共 3,000 元")
        # 逗号右邻是空格、左邻是数字 —— 两侧都不是中文，保持半角
        self.assertEqual(shape.to_fullwidth("v1.2, 版本"), "v1.2, 版本")

    def test_protects_urls(self):
        s = "看这个:https://a.com/x?y=1,2 挺好"
        self.assertIn("https://a.com/x?y=1,2", shape.to_fullwidth(s))
        self.assertIn("看这个：", shape.to_fullwidth(s))

    def test_protects_inline_code(self):
        s = "代码 `a = {1: 2, 3: 4}` 就这样"
        self.assertIn("`a = {1: 2, 3: 4}`", shape.to_fullwidth(s))

    def test_protects_fenced_code(self):
        s = "看:\n```\nd = {'a': 1, 'b': 2}\n```\n懂了吗?"
        out = shape.to_fullwidth(s)
        self.assertIn("{'a': 1, 'b': 2}", out)
        self.assertTrue(out.endswith("懂了吗？"))

    def test_consecutive_punctuation_chains(self):
        self.assertEqual(shape.to_fullwidth("什么?!"), "什么？！")

    def test_semicolon_and_exclamation(self):
        self.assertEqual(shape.to_fullwidth("好的;真棒!"), "好的；真棒！")

    def test_empty_and_none(self):
        self.assertEqual(shape.to_fullwidth(""), "")
        self.assertEqual(shape.to_fullwidth(None), "")

    def test_period_is_left_alone(self):
        # 句号刻意不转：小数点/版本号/英文缩写误伤面太大
        self.assertEqual(shape.to_fullwidth("好的."), "好的.")

    def test_emoji_neighbour(self):
        self.assertEqual(shape.to_fullwidth("好的🐶,我看看"), "好的🐶，我看看")

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
