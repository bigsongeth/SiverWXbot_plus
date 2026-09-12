# -*- coding: utf-8 -*-
"""plugins/ai_news_note/sender.py 里「找笔记编辑器」的单测。

2026-09-12 微信把新建笔记从独立顶层窗口改成了贴在主窗口右侧的内嵌面板，
老判据（顶层窗口 + 标题叫「笔记」+ hwnd 是新的）三条全废。这里把新老两种
形态都锁住，以后微信再变能第一时间看出来是哪一种没认出来。

纯 mock，不碰微信。mac 上直接跑：
    PYTHONPATH=. python3 tests/test_ai_news_editor.py
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for name in ("win32clipboard", "win32gui", "win32con", "win32api", "win32process"):
    sys.modules.setdefault(name, types.ModuleType(name))
# 实现里会用到 win32con.WM_CLOSE，空 stub 取不到会被 except 吞掉，
# 表现成"PostMessage 压根没被调用"，白查半天。
sys.modules["win32con"].WM_CLOSE = 0x0010

_wx = types.ModuleType("wxautox4")
_uia = types.ModuleType("wxautox4.uia")
_uia.uiautomation = types.ModuleType("uiautomation")
_wx.uia = _uia
sys.modules.setdefault("wxautox4", _wx)
sys.modules.setdefault("wxautox4.uia", _uia)

from plugins.ai_news_note import sender as S   # noqa: E402


class Rect(object):
    def __init__(self, l=0, t=0, r=100, b=100):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class FakeCtrl(object):
    """够用的假 UIA 控件：只实现 sender 真正会读的那几个属性。"""

    def __init__(self, ClassName="", Name="", ControlTypeName="PaneControl",
                 hwnd=0, rect=None, children=None):
        self.ClassName = ClassName
        self.Name = Name
        self.ControlTypeName = ControlTypeName
        self.NativeWindowHandle = hwnd
        self.BoundingRectangle = rect or Rect()
        self._children = list(children or [])

    def GetChildren(self):
        return list(self._children)


def make_panel(doc_name="笔记", hwnd=36570558):
    """新形态：主窗口 -> ... -> Chrome_WidgetWin_0 宿主 -> DocumentControl('笔记')"""
    doc = FakeCtrl(ControlTypeName="DocumentControl", Name=doc_name,
                   rect=Rect(1492, 81, 1928, 1036))
    render = FakeCtrl(ClassName="Chrome_RenderWidgetHostHWND",
                      Name="Chrome Legacy Window", hwnd=13173428, children=[doc])
    host = FakeCtrl(ClassName="Chrome_WidgetWin_0", Name="微信",
                    ControlTypeName="PaneControl", hwnd=hwnd,
                    rect=Rect(1492, 33, 1928, 1036), children=[render])
    return FakeCtrl(ClassName="mmui::MainWindow", Name="微信", hwnd=5046434,
                    rect=Rect(8, 0, 1932, 1040),
                    children=[FakeCtrl(ClassName="mmui::XStackedWidget",
                                       children=[host])]), host, doc


def make_window(hwnd=380634098):
    """老形态：桌面顶层有一个 Chrome_WidgetWin_0 且标题就叫「笔记」"""
    doc = FakeCtrl(ControlTypeName="DocumentControl", Name="",
                   rect=Rect(618, 241, 1303, 833))
    return FakeCtrl(ClassName="Chrome_WidgetWin_0", Name="笔记", hwnd=hwnd,
                    rect=Rect(610, 200, 1311, 841), children=[doc])


class EditorFinderTest(unittest.TestCase):
    def setUp(self):
        self._orig_root = getattr(S.auto, "GetRootControl", None)
        self.tops = []
        S.auto.GetRootControl = lambda: FakeCtrl(children=self.tops)

    def tearDown(self):
        if self._orig_root is None:
            try:
                del S.auto.GetRootControl
            except AttributeError:
                pass
        else:
            S.auto.GetRootControl = self._orig_root

    # ---- 老形态 ----
    def test_老形态_顶层新窗口被认出来(self):
        w = make_window(hwnd=111)
        self.tops = [w]
        wx = FakeCtrl(ClassName="mmui::MainWindow", hwnd=5046434)
        host, doc, hwnd, mode = S._find_note_editor(wx, before={999})
        self.assertEqual(mode, "window")
        self.assertEqual(hwnd, 111)
        self.assertIs(host, w)
        self.assertIsNotNone(doc)

    def test_老形态_点击前就存在的旧窗口不认(self):
        w = make_window(hwnd=111)
        self.tops = [w]
        wx = FakeCtrl(ClassName="mmui::MainWindow", hwnd=5046434)
        host, doc, hwnd, mode = S._find_note_editor(wx, before={111})
        self.assertEqual(mode, "")
        self.assertEqual(hwnd, 0)

    def test_老形态_顶层同名窗口但标题不是笔记不认(self):
        self.tops = [FakeCtrl(ClassName="Chrome_WidgetWin_0", Name="微信", hwnd=222)]
        wx = FakeCtrl(ClassName="mmui::MainWindow", hwnd=5046434)
        _, _, hwnd, mode = S._find_note_editor(wx, before=set())
        self.assertEqual((hwnd, mode), (0, ""))

    # ---- 新形态 ----
    def test_新形态_主窗口内嵌面板被认出来(self):
        wx, host, doc = make_panel()
        self.tops = [wx]
        h, d, hwnd, mode = S._find_note_editor(wx, before=set())
        self.assertEqual(mode, "panel")
        self.assertEqual(hwnd, 36570558)
        self.assertIs(h, host)
        self.assertIs(d, doc)

    def test_新形态_hwnd复用时照样认(self):
        """新面板的 hwnd 是固定复用的，不能拿 before 把它挡掉。"""
        wx, host, doc = make_panel(hwnd=36570558)
        self.tops = [wx]
        _, _, hwnd, mode = S._find_note_editor(wx, before={36570558})
        self.assertEqual(mode, "panel")
        self.assertEqual(hwnd, 36570558)

    def test_新形态_正文还没加载出来时不认(self):
        """宿主在了但 DocumentControl('笔记') 还没出来 = 面板没就绪，
        这时候返回成功会让调用方往一个半成品里粘贴。"""
        wx, host, doc = make_panel()
        host._children = []
        self.tops = [wx]
        _, _, hwnd, mode = S._find_note_editor(wx, before=set())
        self.assertEqual((hwnd, mode), (0, ""))

    def test_新形态_文档名不是笔记不认(self):
        wx, host, doc = make_panel(doc_name="朋友圈")
        self.tops = [wx]
        _, _, hwnd, mode = S._find_note_editor(wx, before=set())
        self.assertEqual((hwnd, mode), (0, ""))

    # ---- 两者都在 / 都不在 ----
    def test_老形态优先于新形态(self):
        wx, host, doc = make_panel()
        w = make_window(hwnd=111)
        self.tops = [wx, w]
        _, _, hwnd, mode = S._find_note_editor(wx, before=set())
        self.assertEqual(mode, "window")
        self.assertEqual(hwnd, 111)

    def test_都没有时返回空(self):
        self.tops = [FakeCtrl(ClassName="mmui::MainWindow", hwnd=5046434)]
        wx = self.tops[0]
        _, _, hwnd, mode = S._find_note_editor(wx, before=set())
        self.assertEqual((hwnd, mode), (0, ""))

    def test_主窗口为空不炸(self):
        self.tops = []
        _, _, hwnd, mode = S._find_note_editor(None, before=set())
        self.assertEqual((hwnd, mode), (0, ""))


class PanelHostTest(unittest.TestCase):
    """_find_note_panel_hwnd：给 _close_all_editors 用，关残留的内嵌面板。"""

    def setUp(self):
        self._orig_root = getattr(S.auto, "GetRootControl", None)
        self.tops = []
        S.auto.GetRootControl = lambda: FakeCtrl(children=self.tops)

    def tearDown(self):
        if self._orig_root is None:
            try:
                del S.auto.GetRootControl
            except AttributeError:
                pass
        else:
            S.auto.GetRootControl = self._orig_root

    def test_面板开着时返回它的hwnd(self):
        wx, host, doc = make_panel()
        self.tops = [wx]
        self.assertEqual(S._find_note_panel_hwnd(), 36570558)

    def test_面板没开时返回0(self):
        self.tops = [FakeCtrl(ClassName="mmui::MainWindow", hwnd=5046434)]
        self.assertEqual(S._find_note_panel_hwnd(), 0)

    def test_没有主窗口时返回0(self):
        self.tops = []
        self.assertEqual(S._find_note_panel_hwnd(), 0)


class CloseAllEditorsTest(unittest.TestCase):
    """_close_all_editors 现在还要负责清掉残留的内嵌面板 —— 不清的话，
    下一次建笔记会粘进"上次留下的那块面板"，而它里面可能停着别人的笔记。"""

    def setUp(self):
        self._orig = (S._list_editors, S._find_note_panel_hwnd, S.log,
                      S.time.sleep, getattr(S.win32gui, "PostMessage", None))
        S._list_editors = lambda: set()
        S.log = lambda m: m
        S.time.sleep = lambda n: None
        self.posted = []
        S.win32gui.PostMessage = lambda h, m, w, l: self.posted.append(h)

    def tearDown(self):
        (S._list_editors, S._find_note_panel_hwnd, S.log,
         S.time.sleep, pm) = self._orig
        if pm is None:
            try:
                del S.win32gui.PostMessage
            except AttributeError:
                pass
        else:
            S.win32gui.PostMessage = pm

    def test_没有残留面板时直接通过(self):
        S._find_note_panel_hwnd = lambda: 0
        self.assertEqual(S._close_all_editors(), (True, ""))
        self.assertEqual(self.posted, [])

    def test_残留面板关掉后通过(self):
        seq = [36570558, 0]
        S._find_note_panel_hwnd = lambda: seq.pop(0)
        ok, msg = S._close_all_editors()
        self.assertTrue(ok, msg)
        self.assertEqual(self.posted, [36570558])

    def test_面板关不掉就中止(self):
        S._find_note_panel_hwnd = lambda: 36570558
        ok, msg = S._close_all_editors()
        self.assertFalse(ok)
        self.assertIn("关不掉", msg)

    def test_PostMessage抛异常不往下走成死循环(self):
        seq = [36570558, 0]
        S._find_note_panel_hwnd = lambda: seq.pop(0)
        def boom(*a):
            raise OSError("无效的窗口句柄")
        S.win32gui.PostMessage = boom
        ok, msg = S._close_all_editors()
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
