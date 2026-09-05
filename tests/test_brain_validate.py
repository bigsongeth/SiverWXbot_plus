# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from brain.gateway.config import DEFAULTS
from brain.gateway.validate import validate_reply


class ValidateTest(unittest.TestCase):
    def test_accepts_short_reply_and_strips_markdown(self):
        ok, err = validate_reply(["**大理**还开着"], True, 40, [], 1, DEFAULTS)
        self.assertIsNone(err)
        self.assertEqual(ok, ["大理还开着"])

    def test_rejects_empty(self):
        ok, err = validate_reply([" "], True, 40, [], 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("no_reply", err)

    def test_rejects_too_many_bubbles(self):
        ok, err = validate_reply(["a", "b", "c"], True, 100, [], 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("最多 2 条", err)

    def test_over_budget_first_attempt_rejects_second_truncates(self):
        ok, err = validate_reply(["一" * 60], True, 40, [], 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("预算 40", err)
        ok, err = validate_reply(["一" * 60], True, 40, [], 2, DEFAULTS)
        self.assertIsNone(err); self.assertEqual(ok, ["一" * 40])

    def test_repeat_first_attempt_rejects_second_passes(self):
        recent = ["我刚从键盘上趴起来，刷到消息"]
        ok, err = validate_reply(["我刚从键盘上趴起来，看到你"], False, 100, recent, 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("重复", err)
        ok, err = validate_reply(["我刚从键盘上趴起来，看到你"], False, 100, recent, 2, DEFAULTS)
        self.assertIsNone(err)

    def test_strips_closing_question(self):
        ok, err = validate_reply(["大理还开着。", "要不要我把地址发你？"], True, 100, [], 1, DEFAULTS)
        self.assertEqual(ok, ["大理还开着。"])

    def test_intra_reply_duplicate_rejected_first_attempt_passes_second(self):
        # Attempt 1: should reject duplicate within same reply
        ok, err = validate_reply(["晚上有空吗", "晚上有空吗"], True, 100, [], 1, DEFAULTS)
        self.assertIsNone(ok)
        self.assertIn("重复", err)
        # Attempt 2: should pass duplicate within same reply
        ok, err = validate_reply(["晚上有空吗", "晚上有空吗"], True, 100, [], 2, DEFAULTS)
        self.assertIsNone(err)
        self.assertEqual(len(ok), 2)

    def test_zero_budget_never_returns_empty_accept(self):
        # Attempt 1: budget 0 should mention "预算"
        ok, err = validate_reply(["一" * 10], True, 0, [], 1, DEFAULTS)
        self.assertIsNone(ok)
        self.assertIn("预算", err)
        # Attempt 2: should not return empty list with no error
        ok, err = validate_reply(["一" * 10], True, 0, [], 2, DEFAULTS)
        self.assertFalse(ok == [] and err is None, "Should not return ([], None)")
        # Should either have error or non-empty accepted list
        if err is None:
            self.assertTrue(ok, "If no error, accepted list must be non-empty")
        else:
            self.assertIsNone(ok, "If error exists, ok should be None")


if __name__ == "__main__":
    unittest.main()
