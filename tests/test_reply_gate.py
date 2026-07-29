# -*- coding: utf-8 -*-
"""发送前清洗单测：时间戳剥离 strip_leading_timestamp / 接话闸门 apply_no_reply_gate。

wxbot_core 顶层 import 了 wxautox4 等 Windows 专属库，这里用假模块顶掉，
只测纯函数，不碰微信。
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stub(name, **attrs):
    """向 sys.modules 注入假模块（已存在真模块则不覆盖）。"""
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


class _Anything:
    def __init__(self, *a, **kw):
        pass

    def __getattr__(self, item):
        return _Anything()

    def __call__(self, *a, **kw):
        return _Anything()


for _name in ("schedule", "email_send", "webhook_send"):
    _stub(_name)
# wxbot_core 顶层建了 HTTP = requests.Session()（绕开系统代理的定制点），假模块要有 Session
_stub("requests", Session=_Anything)
_stub("openai", OpenAI=_Anything)
_stub(
    "cozepy",
    COZE_CN_BASE_URL="",
    Coze=_Anything,
    TokenAuth=_Anything,
    Message=_Anything,
    ChatStatus=_Anything,
    MessageContentType=_Anything,
    ChatEventType=_Anything,
)
_stub("wxautox4", WeChat=_Anything, WxParam=_Anything)
_stub("wxautox4.msgs")
_stub("wxautox4.utils")
_stub("wxautox4.utils.useful", check_license=lambda *a, **kw: True)
_stub("logger", log=lambda *a, **kw: None)

from wxbot_core import NO_REPLY_TOKEN, apply_no_reply_gate, strip_leading_timestamp


class StripLeadingTimestampTestCase(unittest.TestCase):
    def test_strips_bracketed_timestamp(self):
        self.assertEqual(
            strip_leading_timestamp("[2026/07/08 19:07:15] 哦 懂了"), "哦 懂了"
        )

    def test_strips_without_brackets(self):
        self.assertEqual(
            strip_leading_timestamp("2026/07/08 19:07:15 哦 懂了"), "哦 懂了"
        )

    def test_strips_dash_date_and_short_time(self):
        self.assertEqual(
            strip_leading_timestamp("[2026-07-08 19:07] 内容"), "内容"
        )

    def test_strips_multiple_stacked_timestamps(self):
        self.assertEqual(
            strip_leading_timestamp("[2026/07/08 19:07:15] [2026/07/08 19:08:00] 内容"),
            "内容",
        )

    def test_keeps_normal_reply(self):
        self.assertEqual(strip_leading_timestamp("正常回复"), "正常回复")

    def test_keeps_timestamp_in_middle(self):
        text = "我们 [2026/07/08 19:07:15] 聊过这个"
        self.assertEqual(strip_leading_timestamp(text), text)

    def test_keeps_plain_date_without_time(self):
        # 只有日期没有时间不像模型模仿的历史格式，不剥离
        text = "2026/07/08 是个好日子"
        self.assertEqual(strip_leading_timestamp(text), text)

    def test_empty_and_none(self):
        self.assertEqual(strip_leading_timestamp(""), "")
        self.assertIsNone(strip_leading_timestamp(None))


class NoReplyGateTestCase(unittest.TestCase):
    def test_token_only_skips(self):
        skip, reply = apply_no_reply_gate(NO_REPLY_TOKEN)
        self.assertTrue(skip)
        self.assertEqual(reply, "")

    def test_token_with_whitespace_skips(self):
        skip, _ = apply_no_reply_gate(f"  {NO_REPLY_TOKEN} \n")
        self.assertTrue(skip)

    def test_token_plus_text_sends_text(self):
        skip, reply = apply_no_reply_gate(f"{NO_REPLY_TOKEN} 其实还是想说一句")
        self.assertFalse(skip)
        self.assertEqual(reply, "其实还是想说一句")

    def test_normal_reply_untouched(self):
        skip, reply = apply_no_reply_gate("正常回复")
        self.assertFalse(skip)
        self.assertEqual(reply, "正常回复")

    def test_empty_reply_untouched(self):
        skip, reply = apply_no_reply_gate("")
        self.assertFalse(skip)
        self.assertEqual(reply, "")


if __name__ == "__main__":
    unittest.main()
