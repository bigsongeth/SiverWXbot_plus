# -*- coding: utf-8 -*-
from __future__ import annotations
import gc
import os
import sys
import time
import unittest
import warnings
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

    def test_stop_closes_pipes_without_resource_warning(self):
        # 覆盖 finding 1：stop() 必须显式关闭 stdin/stdout，否则 TextIOWrapper 触发
        # ResourceWarning: unclosed file。走一遍完整场景（prompt 一次）后 stop()，
        # 强制 gc 一遍，断言没有 ResourceWarning 被记录。
        c = DshClient(FAKE, cwd=os.getcwd(), env=dict(os.environ))
        c.start()
        c.initialize(os.getcwd(), "songkey", "songkey-auto")
        c.prompt("s1", "你好", timeout_sec=5)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c.stop()
            gc.collect()
        resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [], resource_warnings)

    def test_stop_returns_quickly_when_process_ignores_shutdown(self):
        # 覆盖 finding 3：进程卡死（收到 shutdown 请求也不回包）时，stop() 不能被
        # 那次阻塞的 write/等回包拖到永远，必须在约几秒内转入 terminate/kill 强制路径。
        c = DshClient(FAKE, cwd=os.getcwd(), env=dict(os.environ))
        c.start()
        c.initialize(os.getcwd(), "songkey", "songkey-auto")
        c.prompt("s1", "HANG", timeout_sec=1)  # 让子进程卡进 sleep(30)，不再读 stdin
        self.assertTrue(c.alive())
        t0 = time.time()
        c.stop()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0, f"stop() 耗时 {elapsed:.2f}s，应在约 5 秒内返回")
        self.assertFalse(c.alive())

    def test_stop_does_not_block_when_grandchild_holds_stdout(self):
        # 2026-09-06 11:04：dsh 被 kill 后 MCP 孙进程还握着管道写端，读线程拿不到 EOF，
        # stdout.close() 等锁等了 12 分钟。stop() 必须在几秒内返回，宁可漏关一个 fd。
        c = DshClient(FAKE, cwd=os.getcwd(), env=dict(os.environ))
        c.start()
        c.initialize(os.getcwd(), "songkey", "songkey-auto")
        c.prompt("s1", "HANGCHILD", timeout_sec=1)
        t0 = time.time()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            c.stop()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 12.0, f"stop() 耗时 {elapsed:.2f}s")
        self.assertFalse(c.alive())

    def test_turn_error_is_surfaced(self):
        r = self.c.prompt("s1", "TURNERR", timeout_sec=5)
        self.assertEqual(r.error, "boom")
        self.assertFalse(r.timed_out)


if __name__ == "__main__":
    unittest.main()
