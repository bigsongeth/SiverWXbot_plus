# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from brain.gateway import compat


class CompatTest(unittest.TestCase):
    def test_parse_model(self):
        self.assertEqual(compat.parse_model("feirou:group:共建杭州美食地图"), ("共建杭州美食地图", True))
        self.assertEqual(compat.parse_model("feirou:chat:松爸"), ("松爸", False))
        self.assertEqual(compat.parse_model("hzfood-feirou"), ("hzfood-feirou", False))

    def test_parse_last_user(self):
        msgs = [{"role": "user", "content": "A: 早"}, {"role": "assistant", "content": "早"},
                {"role": "user", "content": "[2026-09-05 14:02] 松爸: 西湖有啥"}]
        self.assertEqual(compat.parse_last_user(msgs), ("松爸", "西湖有啥"))
        self.assertEqual(compat.parse_last_user([{"role": "user", "content": "没有前缀"}]), ("", "没有前缀"))
        self.assertEqual(compat.parse_last_user([{"role": "user", "content": [{"type": "text", "text": "K: 图文"}]}]), ("K", "图文"))

    def test_history_to_prime(self):
        msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "A: 早"},
                {"role": "assistant", "content": "早啊"}, {"role": "user", "content": "B: 在吗"}]
        p = compat.history_to_prime(msgs)
        self.assertEqual([(x["attr"], x["sender"], x["content"]) for x in p],
                         [("friend", "A", "早"), ("self", "肥肉", "早啊")])

    def test_to_completion(self):
        c = compat.to_completion({"bubbles": ["a", "b"]}, "m")
        self.assertEqual(c["choices"][0]["message"]["content"], "a||SPLIT||b")
        self.assertEqual(c["object"], "chat.completion")
        self.assertEqual(compat.to_completion({"no_reply": True, "reason": "x"}, "m")["choices"][0]["message"]["content"], "[NO_REPLY]")
        self.assertEqual(compat.to_completion({"error": "busy"}, "m")["choices"][0]["message"]["content"], "API返回错误，请稍后再试")


if __name__ == "__main__":
    unittest.main()
