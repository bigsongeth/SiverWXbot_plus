# -*- coding: utf-8 -*-
"""attach_quote_text：引用消息的被引用人+原文拼进正文。用 ast 把函数摘出来，不 import wxbot_core（mac 上没有 wxautox）。"""
from __future__ import annotations

import ast
import os
import unittest

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wxbot_core.py")


def _load():
    with open(CORE, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "attach_quote_text")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "QUOTE_TEXT_MAX" for t in n.targets))]
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), CORE, "exec"), ns)
    return ns["attach_quote_text"], ns["QUOTE_TEXT_MAX"]


class _Msg:
    def __init__(self, type_, content, quote_content=None, quote_nickname=None):
        self.type, self.content = type_, content
        if quote_content is not None:
            self.quote_content = quote_content
        if quote_nickname is not None:
            self.quote_nickname = quote_nickname


class AttachQuoteTest(unittest.TestCase):
    def setUp(self):
        self.fn, self.max = _load()

    def test_quote_text_appended_with_nick(self):
        m = _Msg("quote", "@🐶肥肉 收录一下这位朋友的推荐～", "这家有朋友推荐过耶", "tomödachį")
        self.fn(m)
        self.assertEqual(m.content, "@🐶肥肉 收录一下这位朋友的推荐～（引用 tomödachį：这家有朋友推荐过耶）")

    def test_idempotent(self):
        m = _Msg("quote", "收一下", "原文", "小A")
        self.fn(m); once = m.content; self.fn(m)
        self.assertEqual(m.content, once)

    def test_non_quote_untouched(self):
        m = _Msg("text", "签到", "不该被用到", "x")
        self.fn(m)
        self.assertEqual(m.content, "签到")

    def test_empty_quote_or_missing_attrs_untouched(self):
        m = _Msg("quote", "收一下", "", "小A"); self.fn(m); self.assertEqual(m.content, "收一下")
        m = _Msg("quote", "收一下"); self.fn(m); self.assertEqual(m.content, "收一下")   # 老版本没这两个字段也不炸

    def test_no_nick(self):
        m = _Msg("quote", "收一下", "原文", "")
        self.fn(m)
        self.assertEqual(m.content, "收一下（引用：原文）")

    def test_image_marker_kept_at_tail(self):
        """message_handle_callback 先拼了 '+引用的图片:路径'，文字标签要插在标记前面，图片路径不能被污染。"""
        m = _Msg("quote", "看这个+引用的图片:C:\\x\\a.jpg", "[图片]", "小B")
        self.fn(m)
        self.assertEqual(m.content, "看这个（引用 小B：[图片]）+引用的图片:C:\\x\\a.jpg")
        self.assertTrue(m.content.split("+引用的图片:", 1)[1].endswith("a.jpg"))

    def test_long_quote_truncated(self):
        m = _Msg("quote", "收一下", "长" * (self.max + 50), "小C")
        self.fn(m)
        self.assertIn("长" * self.max + "…）", m.content)
        self.assertNotIn("长" * (self.max + 1), m.content)


if __name__ == "__main__":
    unittest.main()
