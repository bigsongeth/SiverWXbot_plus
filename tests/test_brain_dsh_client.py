# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import unittest
from brain.gateway.dsh_client import DshClient

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_dsh.py")]


class DshClientTest(unittest.TestCase):
    def setUp(self):
        self.c = DshClient(FAKE, cwd=os.getcwd(), env=dict(os.environ))
        self.c.start()
        self.c.initialize(os.getcwd(), "songkey", "songkey-auto")

    def tearDown(self):
        self.c.stop()

    def test_prompt_returns_text_and_reasoning(self):
        r = self.c.prompt("s1", "你好", timeout_sec=5)
        self.assertEqual(r.text, "echo: 你好")
        self.assertIn("thinking about", r.reasoning)
        self.assertFalse(r.timed_out)
        self.assertIn("turn/end", [e["type"] for e in r.events])

    def test_sessions_do_not_mix(self):
        a = self.c.prompt("a", "A", timeout_sec=5)
        b = self.c.prompt("b", "B", timeout_sec=5)
        self.assertEqual(a.text, "echo: A")
        self.assertEqual(b.text, "echo: B")

    def test_timeout_flag(self):
        r = self.c.prompt("s1", "SLOW", timeout_sec=1)
        self.assertTrue(r.timed_out)

    def test_alive_false_after_crash(self):
        self.c.prompt("s1", "CRASH", timeout_sec=2)
        self.assertFalse(self.c.alive())


if __name__ == "__main__":
    unittest.main()
