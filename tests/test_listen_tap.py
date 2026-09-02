# -*- coding: utf-8 -*-
"""listen_health.tap（开窗录像机）单测：纯 mock，不碰 win32、不碰微信，mac 上直接跑文件。"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("NCC_LOG_SILENT", "1")

from plugins.listen_health import tap  # noqa: E402


class FakeWx:
    def __init__(self, raises=None, result=True, inner=None):
        self.raises = raises
        self.result = result
        self.inner = inner          # AddListenChat 内部要调的"底层函数"（模拟 wxautox 发消息）
        self.calls = []

    def AddListenChat(self, nickname=None, callback=None):
        self.calls.append(nickname)
        if self.inner:
            self.inner()
        if self.raises:
            raise self.raises
        return self.result


class FakeBot:
    def __init__(self, wx):
        self.wx = wx


class Base(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.shots = []
        self._orig = {n: getattr(tap, n) for n in
                      ("_record", "_screenshot", "_wx_top_windows", "_env_snapshot", "load")}
        tap._record = lambda row: self.rows.append(row)
        tap._screenshot = lambda path: (self.shots.append(path), True)[1]
        tap._wx_top_windows = lambda: [{"cls": "Qt51514QWindowIcon", "t": "微信", "v": 1, "r": [0, 0, 1200, 1048]}]
        tap._env_snapshot = lambda: {"fg_title": "微信", "wx_main_rect": [0, 0, 1200, 1048]}
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": True, "keep_success": True}}
        tap.CALLS.clear()
        tap._ACTIVE[0] = False

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(tap, n, v)
        tap.CALLS.clear()
        tap._ACTIVE[0] = False


class TestWrapDecode(Base):
    def test_wrap_records_only_while_active_and_passes_through(self):
        fake = types.SimpleNamespace(SendMessage=lambda h, m, w, l: "ret")
        self.assertTrue(tap.wrap(fake, "SendMessage", "fake.SendMessage"))
        # 未激活：透传、不记录
        self.assertEqual(fake.SendMessage(1, 0x0201, 0, 0), "ret")
        self.assertEqual(tap.CALLS, [])
        # 激活：记录并解码坐标（lParam = y<<16 | x）
        tap._ACTIVE[0] = True
        self.assertEqual(fake.SendMessage(262950, 0x0203, 0, (113 << 16) | 180), "ret")
        self.assertEqual(len(tap.CALLS), 1)
        c = tap.CALLS[0]
        self.assertEqual(c["msg"], "WM_LBUTTONDBLCLK")
        self.assertEqual((c["hwnd"], c["x"], c["y"]), (262950, 180, 113))
        self.assertEqual(c["call"], "fake.SendMessage")

    def test_wrap_keeps_original_exception(self):
        def boom(*a):
            raise OSError("x")
        fake = types.SimpleNamespace(PostMessage=boom)
        tap.wrap(fake, "PostMessage", "fake.PostMessage")
        tap._ACTIVE[0] = True
        with self.assertRaises(OSError):
            fake.PostMessage(1, 0x0201, 0, 0)
        self.assertEqual(len(tap.CALLS), 1)   # 抛异常也记一笔

    def test_decode_negative_client_coords(self):
        c = tap.decode("t", (1, 0x0201, 0, (0xFFFF << 16) | 0xFFF0))
        self.assertEqual((c["x"], c["y"]), (-16, -1))


class TestInstall(Base):
    def test_install_wraps_once(self):
        wx = FakeWx()
        bot = FakeBot(wx)
        self.assertTrue(tap.install(bot))
        first = wx.AddListenChat
        self.assertTrue(getattr(first, "_lh_tap", False))
        tap.install(bot)
        self.assertIs(wx.AddListenChat, first)   # 幂等，不套两层

    def test_disabled_leaves_wx_untouched(self):
        tap.load = lambda: {"tap": {"enabled": False}}
        wx = FakeWx()
        before = wx.AddListenChat
        self.assertFalse(tap.install(FakeBot(wx)))
        self.assertEqual(wx.AddListenChat, before)

    def test_success_records_row_without_screenshot(self):
        wx = FakeWx(result=True)
        tap.install(FakeBot(wx))
        self.assertTrue(wx.AddListenChat(nickname="文件传输助手", callback=None))
        self.assertEqual(wx.calls, ["文件传输助手"])
        self.assertEqual(len(self.rows), 1)
        self.assertTrue(self.rows[0]["ok"])
        self.assertIsNone(self.rows[0]["shot"])
        self.assertEqual(self.shots, [])

    def test_failure_reraises_records_calls_and_screenshot(self):
        fake = types.SimpleNamespace(SendMessage=lambda h, m, w, l: 0)
        tap.wrap(fake, "SendMessage", "win32api.SendMessage")

        def inner():
            fake.SendMessage(656210, 0x0201, 0, (241 << 16) | 180)
            fake.SendMessage(656210, 0x0203, 0, (241 << 16) | 180)

        wx = FakeWx(raises=OSError("error(1400, 'MoveWindow', '无效的窗口句柄。')"), inner=inner)
        tap.install(FakeBot(wx))
        with self.assertRaises(OSError):
            wx.AddListenChat(nickname="😃Pablo😃_🐕", callback=None)
        self.assertEqual(len(self.rows), 1)
        row = self.rows[0]
        self.assertFalse(row["ok"])
        self.assertIn("MoveWindow", row["err"])
        self.assertEqual(row["n_click_msgs"], 2)
        self.assertEqual(row["calls"][1]["msg"], "WM_LBUTTONDBLCLK")
        self.assertEqual((row["calls"][1]["x"], row["calls"][1]["y"]), (180, 241))
        self.assertEqual(len(self.shots), 1)
        self.assertEqual(row["fg"], "微信")
        self.assertFalse(tap._ACTIVE[0])       # 结束后一定关掉录制

    def test_falsy_result_counts_as_failure(self):
        wx = FakeWx(result=False)
        tap.install(FakeBot(wx))
        self.assertFalse(wx.AddListenChat(nickname="x", callback=None))
        self.assertFalse(self.rows[0]["ok"])

    def test_keep_success_false_skips_ok_rows(self):
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "keep_success": False}}
        wx = FakeWx(result=True)
        tap.install(FakeBot(wx))
        wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.rows, [])
        wx.raises = OSError("boom")
        with self.assertRaises(OSError):
            wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(len(self.rows), 1)
        self.assertIsNone(self.rows[0]["shot"])   # screenshot=False


if __name__ == "__main__":
    unittest.main()
