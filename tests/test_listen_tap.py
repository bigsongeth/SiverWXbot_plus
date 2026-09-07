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
                      ("_record", "_screenshot", "_wx_top_windows", "_env_snapshot", "load",
                       "_cursor_pos", "_set_cursor_pos", "_point_owner_is_wechat")}
        # 指针守卫默认按"指针在别的程序上"处理，不触发；要测守卫的用例自己改这三个桩
        tap._cursor_pos = lambda: (1700, 500)
        tap._point_owner_is_wechat = lambda x, y: False
        tap._set_cursor_pos = lambda pt: None
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


class TestUnstickRetry(Base):
    """开窗失败（MoveWindow 1400）→ 复位动作 → 原地重试一次。"""

    def setUp(self):
        super().setUp()
        self.unsticks = []
        self._orig_unstick = tap.unstick
        tap.unstick = lambda mode, hwnd=None, points=None: (self.unsticks.append(mode), "已点")[1]

    def tearDown(self):
        tap.unstick = self._orig_unstick
        super().tearDown()

    def _wx_fail_then_ok(self):
        state = {"n": 0}

        class Wx:
            def AddListenChat(_s, nickname=None, callback=None):
                state["n"] += 1
                if state["n"] == 1:
                    raise OSError("error(1400, 'MoveWindow', '无效的窗口句柄。')")
                return True
        return Wx(), state

    def test_movewindow_failure_triggers_unstick_and_retry(self):
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "unstick": "click"}}
        wx, state = self._wx_fail_then_ok()
        tap.install(FakeBot(wx))
        self.assertTrue(wx.AddListenChat(nickname="x", callback=None))   # 对调用方透明：直接拿到成功
        self.assertEqual(self.unsticks, ["click"])
        self.assertEqual(state["n"], 2)
        # 两行记录：复位前的失败 + 重试后的成功
        self.assertEqual([r["ok"] for r in self.rows], [False, True])
        self.assertEqual(self.rows[0]["note"], "复位前的那次")
        self.assertFalse(tap._ACTIVE[0])

    def test_other_errors_do_not_unstick(self):
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "unstick": "click"}}
        wx = FakeWx(raises=LookupError("Find Control Timeout"))
        tap.install(FakeBot(wx))
        with self.assertRaises(LookupError):
            wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.unsticks, [])
        self.assertEqual(len(wx.calls), 1)

    def test_unstick_disabled_by_empty_mode(self):
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "unstick": ""}}
        wx, state = self._wx_fail_then_ok()
        tap.install(FakeBot(wx))
        with self.assertRaises(OSError):
            wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.unsticks, [])
        self.assertEqual(state["n"], 1)

    def test_retry_failure_reraises(self):
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "unstick": "minmax"}}
        wx = FakeWx(raises=OSError("error(1400, 'MoveWindow', '无效的窗口句柄。')"))
        tap.install(FakeBot(wx))
        with self.assertRaises(OSError):
            wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.unsticks, ["minmax"])
        self.assertEqual(len(wx.calls), 2)
        self.assertEqual([r["ok"] for r in self.rows], [False, False])

    def test_unstick_exception_does_not_mask_original(self):
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "unstick": "click"}}
        tap.unstick = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no wechat"))
        wx, state = self._wx_fail_then_ok()
        tap.install(FakeBot(wx))
        with self.assertRaises(OSError):
            wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(state["n"], 1)


class TestPostClicks(Base):
    """实验开关 post_clicks：AddListenChat 期间把 SendMessage 的鼠标消息改成 PostMessage。"""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.posted = []
        self.fake = types.SimpleNamespace(
            SendMessage=lambda h, m, w, l: self.sent.append((h, m)) or "sent",
            PostMessage=lambda h, m, w, l: self.posted.append((h, m)) or 1,
        )
        tap._ORIG_POST["fake"] = self.fake.PostMessage
        tap.wrap(self.fake, "PostMessage", "fake.PostMessage")
        tap.wrap(self.fake, "SendMessage", "fake.SendMessage")

    def tearDown(self):
        tap._ORIG_POST.pop("fake", None)
        tap._POST_CLICKS[0] = False
        super().tearDown()

    def test_off_by_default_passes_through(self):
        tap._ACTIVE[0] = True
        self.assertEqual(self.fake.SendMessage(1, 0x0203, 0, 0), "sent")
        self.assertEqual(self.posted, [])
        self.assertEqual(tap.CALLS[0]["call"], "fake.SendMessage")

    def test_on_converts_only_click_messages_while_active(self):
        tap._POST_CLICKS[0] = True
        # 未激活：哪怕开关开着也原样 SendMessage（别的 UI 操作不受影响）
        self.assertEqual(self.fake.SendMessage(1, 0x0203, 0, 0), "sent")
        self.assertEqual(self.posted, [])
        tap._ACTIVE[0] = True
        self.assertEqual(self.fake.SendMessage(262950, 0x0201, 1, (497 << 16) | 180), 0)
        self.assertEqual(self.fake.SendMessage(262950, 0x0203, 1, (497 << 16) | 180), 0)
        self.assertEqual(self.fake.SendMessage(262950, 0x0010, 0, 0), "sent")   # WM_CLOSE 不转
        self.assertEqual(self.posted, [(262950, 0x0201), (262950, 0x0203)])
        self.assertEqual(self.sent, [(1, 0x0203), (262950, 0x0010)])
        tags = [c["call"] for c in tap.CALLS]
        self.assertEqual(tags, ["fake.SendMessage→PostMessage", "fake.SendMessage→PostMessage", "fake.SendMessage"])
        self.assertEqual((tap.CALLS[0]["x"], tap.CALLS[0]["y"]), (180, 497))

    def test_switch_is_hot_read_from_config_on_each_call(self):
        tap.load = lambda: {"tap": {"enabled": True, "post_clicks": True, "screenshot": False}}

        def inner():
            self.fake.SendMessage(5, 0x0203, 0, 0)

        wx = FakeWx(result=True, inner=inner)
        tap.install(FakeBot(wx))
        wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.posted, [(5, 0x0203)])
        self.assertFalse(tap._ACTIVE[0])
        # 配置改回 False，下一次调用立刻不转
        tap.load = lambda: {"tap": {"enabled": True, "post_clicks": False, "screenshot": False}}
        wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.posted, [(5, 0x0203)])
        self.assertEqual(self.sent[-1], (5, 0x0203))


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




MAIN = {"cls": "Qt51514QWindowIcon", "t": "微信", "v": 1, "r": [0, 0, 1500, 1048]}
SUB = {"cls": "Qt51514QWindowIcon", "t": "🏜️AI 及其代理人联邦🐶", "v": 1, "r": [0, 0, 1500, 1048]}
TRAY = {"cls": "Qt51514QWindowIcon", "t": "Weixin", "v": 0, "r": [872, 409, 1048, 608]}


class TestCursorDanger(unittest.TestCase):
    """2026-09-08 实测：指针在窗口顶边 +0/+2 双击必败，+10 就好；子窗口顶边同样有毒；窗口内部/Chrome/任务栏都安全。"""

    def test_top_edge_of_main_is_danger(self):
        self.assertIs(tap.cursor_danger((1098, 0), [MAIN, TRAY]), MAIN)
        self.assertIs(tap.cursor_danger((1098, 2), [MAIN]), MAIN)
        self.assertIs(tap.cursor_danger((300, 0), [MAIN]), MAIN)

    def test_inside_window_is_safe(self):
        for pt in ((1098, 10), (1098, 30), (700, 600), (608, 900)):
            self.assertIsNone(tap.cursor_danger(pt, [MAIN]), pt)

    def test_sub_window_on_top_uses_its_edge(self):
        # 子窗口盖在主窗口上（同 rect），Z 序第一个说了算
        self.assertIs(tap.cursor_danger((700, 2), [SUB, MAIN]), SUB)
        self.assertIsNone(tap.cursor_danger((700, 600), [SUB, MAIN]))

    def test_other_edges_are_conservatively_danger(self):
        self.assertIs(tap.cursor_danger((0, 500), [MAIN]), MAIN)
        self.assertIs(tap.cursor_danger((1499, 500), [MAIN]), MAIN)
        self.assertIs(tap.cursor_danger((700, 1047), [MAIN]), MAIN)

    def test_outside_all_windows_and_invisible_ignored(self):
        self.assertIsNone(tap.cursor_danger((1700, 500), [MAIN, TRAY]))
        self.assertIsNone(tap.cursor_danger((900, 410), [TRAY]))       # 不可见/小窗口不算
        self.assertIsNone(tap.cursor_danger(None, [MAIN]))
        self.assertIsNone(tap.cursor_danger((1, 1), []))

    def test_band_is_configurable(self):
        self.assertIsNone(tap.cursor_danger((1098, 10), [MAIN], band=8))
        self.assertIs(tap.cursor_danger((1098, 10), [MAIN], band=12), MAIN)

    def test_safe_park_point_inside_main_away_from_edges(self):
        pt = tap.safe_park_point([SUB, MAIN, TRAY])
        self.assertEqual(pt, (600, 890))
        self.assertIsNone(tap.cursor_danger(pt, [SUB, MAIN]))
        self.assertIsNone(tap.safe_park_point([TRAY]))
        # 没有主窗口就用第一个大窗口
        self.assertEqual(tap.safe_park_point([SUB]), (600, 890))


class TestCursorGuardInWrapper(Base):
    def setUp(self):
        super().setUp()
        self.moves = []
        self.order = []
        tap._wx_top_windows = lambda: [SUB, MAIN, TRAY]
        tap._set_cursor_pos = lambda pt: (self.moves.append(pt), self.order.append("move"))
        tap.load = lambda: {"tap": {"enabled": True, "screenshot": False, "keep_success": True, "unstick": ""}}

    def _wx(self):
        wx = FakeWx(inner=lambda: self.order.append("open"))
        return wx

    def test_moves_cursor_off_top_edge_before_opening(self):
        tap._cursor_pos = lambda: (1098, 0)
        tap._point_owner_is_wechat = lambda x, y: True
        wx = self._wx()
        tap.wrap_add_listen_chat(wx, {"cursor_guard": True})
        self.assertTrue(wx.AddListenChat(nickname="共建杭州美食地图🐶", callback=None))
        self.assertEqual(self.moves, [(600, 890)])
        self.assertEqual(self.order, ["move", "open"])       # 先挪指针，再开窗
        self.assertEqual(self.rows[-1]["cursor_guard"], {"from": [1098, 0], "to": [600, 890], "win": SUB["t"]})

    def test_cursor_inside_window_untouched(self):
        tap._cursor_pos = lambda: (700, 600)
        tap._point_owner_is_wechat = lambda x, y: True
        wx = self._wx()
        tap.wrap_add_listen_chat(wx, {"cursor_guard": True})
        wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.moves, [])
        self.assertIsNone(self.rows[-1]["cursor_guard"])

    def test_cursor_on_other_app_untouched_even_if_geometry_overlaps(self):
        tap._cursor_pos = lambda: (1098, 0)
        tap._point_owner_is_wechat = lambda x, y: False       # Chrome 盖在上面
        wx = self._wx()
        tap.wrap_add_listen_chat(wx, {"cursor_guard": True})
        wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.moves, [])

    def test_guard_can_be_disabled(self):
        tap._cursor_pos = lambda: (1098, 0)
        tap._point_owner_is_wechat = lambda x, y: True
        wx = self._wx()
        tap.wrap_add_listen_chat(wx, {"cursor_guard": False})
        wx.AddListenChat(nickname="x", callback=None)
        self.assertEqual(self.moves, [])

    def test_guard_failure_never_blocks_open(self):
        def boom():
            raise RuntimeError("no win32")
        tap._cursor_pos = boom
        wx = self._wx()
        tap.wrap_add_listen_chat(wx, {"cursor_guard": True})
        self.assertTrue(wx.AddListenChat(nickname="x", callback=None))
        self.assertEqual(self.order, ["open"])


if __name__ == "__main__":
    unittest.main()
