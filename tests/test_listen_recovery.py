# -*- coding: utf-8 -*-
"""
僵尸监听注册的自恢复测试。

背景（2026-08-15）：面板点「停止/启动机器人」只重启 bot 线程、进程不换，
wxautox 41.x 的 WeChat() 会走缓存复活同一个实例（日志 `使用缓存初始化，HWND=...`），
上一轮已经失效的监听注册跟着留下。之后每次 AddListenChat 都被判
`chat already has a listener` 直接拒绝——旧窗口是死的、新窗口不给建，
四个群（含管理群）整晚收不到消息，群消息只在 GetNextNewMessage 的全局私聊扫描里
露一面就被「私聊全局监听收到群聊消息，跳过」丢掉。

修复：确认子窗口不存在 + wxautox 报「已在监听」时，先 RemoveListenChat 清掉僵尸注册再重加。

不 import wxbot_core（会连带拉起 wxautox，mac 上跑不了），用 ast 把 WXBot 的几个方法
摘出来绑到一个假类上。跑法：PYTHONPATH=. python3 tests/test_listen_recovery.py
"""
import ast
import os
import sys
import types
import unittest

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wxbot_core.py')

METHODS = [
    '_listen_add_error',
    '_is_already_listening',
    '_close_orphan_chat_window',
    '_purge_stale_listener',
    '_subwindow_who',
    '_try_get_all_subwindow_names',
    '_get_all_subwindow_names',
    '_get_verified_subwindow',
    '_add_listen_chat_once',
    '_verify_initial_listeners',
    '_add_and_verify_subwindow',
]

# 最终失败时 _add_and_verify_subwindow 会 import listen_health 推告警，
# 这里塞个假模块，免得单测真去发 webhook。
_fake_plugin = types.ModuleType('plugins.listen_health')
_fake_plugin.alert_listen_failure = lambda *a, **kw: None
sys.modules.setdefault('plugins', types.ModuleType('plugins'))
sys.modules['plugins.listen_health'] = _fake_plugin


class FakeWin32Gui(types.ModuleType):
    """
    假的 win32gui：mac 上没有 pywin32，而关残留独立窗口这条路必须测
    （2026-08-15 的真凶就在这条路上）。windows 是 {hwnd: 标题}。
    """

    def __init__(self, windows=None):
        super().__init__('win32gui')
        self.windows = dict(windows or {})
        self.closed = []

    def EnumWindows(self, callback, extra):
        for hwnd in list(self.windows):
            callback(hwnd, extra)

    def GetWindowText(self, hwnd):
        return self.windows.get(hwnd, '')

    def PostMessage(self, hwnd, msg, wparam, lparam):
        self.closed.append(hwnd)
        self.windows.pop(hwnd, None)


def install_win32(windows=None):
    """装上假的 win32gui/win32con，返回那个假模块。"""
    fake = FakeWin32Gui(windows)
    con = types.ModuleType('win32con')
    con.WM_CLOSE = 0x0010
    sys.modules['win32gui'] = fake
    sys.modules['win32con'] = con
    return fake


def uninstall_win32():
    sys.modules.pop('win32gui', None)
    sys.modules.pop('win32con', None)

LOGS = []


def _fake_log(level=None, message=None):
    LOGS.append(f'{level}: {message}')


CONSTS = ['MAIN_WINDOW_TITLES', 'RETRY_BACKOFF']


def _load_methods():
    """从 wxbot_core.py 的 WXBot 里摘方法和类常量出来 exec，不触发模块级 import。"""
    with open(CORE, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'WXBot')
    wanted = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    assert len(wanted) == len(METHODS), f'少摘到方法: {set(METHODS) - {n.name for n in wanted}}'
    # 类常量跟着源码走，别在测试里另抄一份
    consts = [n for n in cls.body if isinstance(n, ast.Assign)
              and any(getattr(t, 'id', None) in CONSTS for t in n.targets)]
    assert len(consts) == len(CONSTS), f'少摘到类常量: {CONSTS}'
    ns = {'log': _fake_log, 'time': types.SimpleNamespace(sleep=lambda s: None)}
    exec(compile(ast.Module(body=consts + wanted, type_ignores=[]), CORE, 'exec'), ns)
    return ns


NS = _load_methods()


class Bot:
    """只带被测方法的假 WXBot。"""
    RETRY_BACKOFF = (0, 0, 0)   # 退避间隔在测试里没意义，time.sleep 也已经被换掉

    def __init__(self, wx):
        self.wx = wx
        self._last_listen_error = None
        self.message_handle_callback = lambda *a, **kw: None


for _name in METHODS:
    setattr(Bot, _name, NS[_name])
Bot.MAIN_WINDOW_TITLES = NS['MAIN_WINDOW_TITLES']


class SubWindow:
    def __init__(self, who):
        self.who = who


class WxResponse(dict):
    """
    仿 wxautox 的返回值：是个 dict，但真假值看 status——
    生产代码里 `if result:` 判成败靠的就是这个，普通 dict 恒真会把测试测歪。
    """

    def __bool__(self):
        return self.get('status') == '成功'


class FakeWX:
    """
    模拟 wxautox：内部有一份 listen 注册表，还有一份真实存在的子窗口集合。
    僵尸注册 = 名字在 registry 里、但不在 windows 里。
    """

    def __init__(self, registry=(), windows=(), heal_on_readd=True):
        self.registry = set(registry)
        self.windows = set(windows)
        self.heal_on_readd = heal_on_readd   # 清掉僵尸注册后重加能不能真建出窗口
        self.add_calls = []
        self.remove_calls = []

    def AddListenChat(self, nickname, callback=None):
        self.add_calls.append(nickname)
        if nickname in self.registry:
            return WxResponse(status='失败', message='chat already has a listener')
        self.registry.add(nickname)
        if self.heal_on_readd:
            self.windows.add(nickname)
        return WxResponse(status='成功')

    def RemoveListenChat(self, nickname, close_window=True):
        self.remove_calls.append(nickname)
        self.registry.discard(nickname)
        self.windows.discard(nickname)
        return WxResponse(status='成功')

    def GetAllSubWindow(self):
        return [SubWindow(w) for w in sorted(self.windows)]

    def GetSubWindow(self, nickname):
        return SubWindow(nickname) if nickname in self.windows else None


class TestIsAlreadyListening(unittest.TestCase):

    def test_success_result_is_not_already_listening(self):
        bot = Bot(FakeWX())
        self.assertFalse(bot._is_already_listening(WxResponse(status='成功')))

    def test_recognizes_already_has_a_listener(self):
        bot = Bot(FakeWX())
        self.assertTrue(bot._is_already_listening(WxResponse(status='失败', message='chat already has a listener')))

    def test_other_failures_are_not_touched(self):
        """窗口句柄无效属于另一类故障，不能去清注册表。"""
        bot = Bot(FakeWX())
        self.assertFalse(bot._is_already_listening(WxResponse(status='失败', message="error(1400, 'MoveWindow', ...)")))

    def test_none_result_is_not_already_listening(self):
        """AddListenChat 抛异常时上层返回 None，不该被当成僵尸注册。"""
        bot = Bot(FakeWX())
        self.assertFalse(bot._is_already_listening(None))


class TestPurgeStaleListener(unittest.TestCase):

    def tearDown(self):
        uninstall_win32()

    def test_purge_calls_remove(self):
        wx = FakeWX(registry=['某群'])
        bot = Bot(wx)
        self.assertTrue(bot._purge_stale_listener('某群'))
        self.assertEqual(wx.remove_calls, ['某群'])
        self.assertNotIn('某群', wx.registry)

    def test_purge_swallows_exception(self):
        """RemoveListenChat 抛异常也要接着去关残留窗口，不能直接放弃。"""
        class Boom(FakeWX):
            def RemoveListenChat(self, nickname, close_window=True):
                raise RuntimeError('boom')

        gui = install_win32({100: '某群'})
        bot = Bot(Boom(registry=['某群']))
        self.assertTrue(bot._purge_stale_listener('某群'))
        self.assertEqual(gui.closed, [100])

    def test_never_closes_window_getsubwindow_still_owns(self):
        """
        GetAllSubWindow 漏报、GetSubWindow 却认得这个子窗口时不许关——
        关窗口不可逆，而本机「弹不出新独立窗口」是老毛病，关错就彻底没得救。
        """
        gui = install_win32({16909862: '肥肉测试1🐶'})
        wx = FakeWX(registry=['肥肉测试1🐶'], windows=['肥肉测试1🐶'])
        Bot(wx)._purge_stale_listener('肥肉测试1🐶')
        self.assertEqual(gui.closed, [])

    def test_purge_returns_false_when_nothing_to_do(self):
        class NotFound(FakeWX):
            def RemoveListenChat(self, nickname, close_window=True):
                raise RuntimeError('未找到监听对象')

        install_win32({})
        self.assertFalse(Bot(NotFound())._purge_stale_listener('某群'))


class TestCloseOrphanChatWindow(unittest.TestCase):
    """
    2026-08-15 真凶：失败的两个群各有一个独立聊天窗口还开着且被最小化
    （会话 2 枚举到 rect=-32000），wxautox 见窗口在就回「已在监听」拒绝重建，
    可 RemoveListenChat 又说「未找到监听对象」——只清注册表没用，得关窗口。
    """

    def tearDown(self):
        uninstall_win32()

    def test_closes_matching_window(self):
        gui = install_win32({16909862: '肥肉测试1🐶', 197366: '微信'})
        self.assertTrue(Bot(FakeWX())._close_orphan_chat_window('肥肉测试1🐶'))
        self.assertEqual(gui.closed, [16909862])

    def test_never_closes_main_window(self):
        gui = install_win32({197366: '微信'})
        self.assertFalse(Bot(FakeWX())._close_orphan_chat_window('微信'))
        self.assertEqual(gui.closed, [])

    def test_skips_main_window_handle(self):
        """同名兜底：主窗口句柄一律跳过。"""
        gui = install_win32({197366: '某群'})
        bot = Bot(FakeWX())
        bot.wx.HWND = 197366
        self.assertFalse(bot._close_orphan_chat_window('某群'))
        self.assertEqual(gui.closed, [])

    def test_no_orphan_window(self):
        gui = install_win32({197366: '微信'})
        self.assertFalse(Bot(FakeWX())._close_orphan_chat_window('某群'))
        self.assertEqual(gui.closed, [])

    def test_without_pywin32_is_harmless(self):
        """mac / 没装 pywin32 的环境下只记一行日志，不能炸。"""
        uninstall_win32()
        self.assertFalse(Bot(FakeWX())._close_orphan_chat_window('某群'))


class TestVerifyInitialListeners(unittest.TestCase):
    """初始化路径：四个群卡在 already has a listener，必须能自己恢复。"""

    GROUPS = ['NCC 社群管理肥肉售后维权🤖', '🏜️AI 及其代理人联邦🐶', '肥肉测试1🐶', '爱和一切肥肉测试群']

    def test_stale_registry_is_purged_and_relistened(self):
        wx = FakeWX(registry=self.GROUPS, windows=[])   # 注册表有、窗口全没 = 僵尸注册
        bot = Bot(wx)
        bot._verify_initial_listeners(list(self.GROUPS))
        self.assertEqual(wx.windows, set(self.GROUPS))
        for g in self.GROUPS:
            self.assertIn(g, wx.remove_calls)

    def test_first_round_is_enough(self):
        """清一次就该好，别再多打两轮重试。"""
        wx = FakeWX(registry=self.GROUPS, windows=[])
        bot = Bot(wx)
        bot._verify_initial_listeners(list(self.GROUPS))
        # 每个群：重试加一次(被拒) + 清理后重加一次 = 2 次
        self.assertEqual(len(wx.add_calls), 2 * len(self.GROUPS))

    def test_live_listeners_are_not_purged(self):
        """子窗口都在时压根不进重试，更不该去 RemoveListenChat。"""
        wx = FakeWX(registry=self.GROUPS, windows=self.GROUPS)
        bot = Bot(wx)
        bot._verify_initial_listeners(list(self.GROUPS))
        self.assertEqual(wx.remove_calls, [])
        self.assertEqual(wx.add_calls, [])

    def test_other_failure_does_not_purge(self):
        """MoveWindow 1400 这类失败不碰注册表，行为跟改动前一致。"""
        class Handle1400(FakeWX):
            def AddListenChat(self, nickname, callback=None):
                self.add_calls.append(nickname)
                return WxResponse(status='失败', message="error(1400, 'MoveWindow', '无效的窗口句柄。')")

        wx = Handle1400(registry=['松爸'], windows=[])
        bot = Bot(wx)
        bot._verify_initial_listeners(['松爸'])
        self.assertEqual(wx.remove_calls, [])

    def test_gives_up_when_rebuild_still_fails(self):
        """清完还是建不出窗口时，照旧走完重试次数再放弃，不能死循环。"""
        wx = FakeWX(registry=['某群'], windows=[], heal_on_readd=False)
        bot = Bot(wx)
        bot._verify_initial_listeners(['某群'], retry_count=2)
        self.assertEqual(wx.windows, set())
        self.assertTrue(any('已跳过实际监听' in line for line in LOGS[-3:]))


class TestWindowGatedRefusal(unittest.TestCase):
    """
    最贴近 2026-08-15 现场的一条：wxautox 的拒绝来自「窗口还在」，
    注册表是空的（RemoveListenChat 回未找到），所以只有把窗口关掉才建得回来。
    """

    def tearDown(self):
        uninstall_win32()

    class WindowGatedWX(FakeWX):
        """AddListenChat 的成败取决于残留窗口在不在，注册表始终是空的。"""

        def __init__(self, gui, orphan_hwnd, nickname):
            super().__init__()
            self.gui = gui
            self.orphan_hwnd = orphan_hwnd
            self.nickname = nickname

        def AddListenChat(self, nickname, callback=None):
            self.add_calls.append(nickname)
            if self.orphan_hwnd in self.gui.windows:
                return WxResponse(status='失败', message='chat already has a listener')
            self.windows.add(nickname)
            return WxResponse(status='成功')

        def RemoveListenChat(self, nickname, close_window=True):
            self.remove_calls.append(nickname)
            return WxResponse(status='失败', message='未找到监听对象')

    def test_recovers_after_closing_orphan_window(self):
        gui = install_win32({16909862: '肥肉测试1🐶'})
        wx = self.WindowGatedWX(gui, 16909862, '肥肉测试1🐶')
        bot = Bot(wx)
        bot._verify_initial_listeners(['肥肉测试1🐶'])
        self.assertEqual(gui.closed, [16909862])
        self.assertIn('肥肉测试1🐶', wx.windows)

    def test_dynamic_listen_recovers_too(self):
        gui = install_win32({16909862: 'King_🐕'})
        wx = self.WindowGatedWX(gui, 16909862, 'King_🐕')
        sub = Bot(wx)._add_and_verify_subwindow('King_🐕')
        self.assertIsNotNone(sub)
        self.assertEqual(gui.closed, [16909862])


class TestAddAndVerifySubwindow(unittest.TestCase):
    """动态监听路径（全局模式下新私聊）同样会撞上僵尸注册。"""

    def test_stale_registry_recovers_on_first_attempt(self):
        wx = FakeWX(registry=['King_🐕'], windows=[])
        bot = Bot(wx)
        sub = bot._add_and_verify_subwindow('King_🐕')
        self.assertIsNotNone(sub)
        self.assertEqual(sub.who, 'King_🐕')
        self.assertEqual(wx.remove_calls, ['King_🐕'])

    def test_healthy_add_does_not_purge(self):
        wx = FakeWX()
        bot = Bot(wx)
        self.assertIsNotNone(bot._add_and_verify_subwindow('新朋友'))
        self.assertEqual(wx.remove_calls, [])

    def test_returns_none_when_unrecoverable(self):
        wx = FakeWX(registry=['某人'], windows=[], heal_on_readd=False)
        bot = Bot(wx)
        self.assertIsNone(bot._add_and_verify_subwindow('某人', retry_count=1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
