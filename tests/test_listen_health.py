# -*- coding: utf-8 -*-
"""
listen_health 插件单测。纯 mock，不碰微信、不发请求。
跑法：PYTHONPATH=. python3 tests/test_listen_health.py
"""
import ast
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.listen_health import alert as alert_mod
from plugins.listen_health import probe as probe_mod
from plugins.listen_health import config as cfg_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeWx:
    """够用的 wxautox 替身：可配置 AddListenChat 抛不抛异常、GetSubWindow 给不给窗口。"""

    def __init__(self, add_raises=None, sub=object(), subwindows=None):
        self.add_raises = add_raises
        self.sub = sub
        self.subwindows = subwindows if subwindows is not None else []
        self.removed = []

    def AddListenChat(self, nickname, callback):
        if self.add_raises:
            raise self.add_raises
        return True

    def GetSubWindow(self, nickname):
        return self.sub

    def GetAllSubWindow(self):
        return self.subwindows

    def RemoveListenChat(self, nickname):
        self.removed.append(nickname)
        return True


class FakeBot:
    def __init__(self, wx=None):
        self.wx = wx or FakeWx()
        self.all_Mode_listen_list = []
        self.msg_received_count = 7


class TestAlert(unittest.TestCase):

    def setUp(self):
        alert_mod._last_alert.clear()

    def test_message_contains_key_facts(self):
        msg = alert_mod.build_message('King_🐕', 3, "error(1400, 'MoveWindow', ...)", window_count=8)
        self.assertIn('King_🐕', msg)
        self.assertIn('3 次', msg)
        self.assertIn('1400', msg)
        self.assertIn('8 个', msg)
        self.assertIn('不会丢', msg)

    def test_message_without_exception_says_so(self):
        """AddListenChat 没抛异常但校验没过，也要说清楚，不要显示成 None。"""
        msg = alert_mod.build_message('张三', 3, None)
        self.assertIn('无（', msg)
        self.assertNotIn('None', msg)

    def test_cooldown_blocks_second_alert(self):
        self.assertTrue(alert_mod._should_alert('A', 600, 1000.0))
        self.assertFalse(alert_mod._should_alert('A', 600, 1100.0))

    def test_cooldown_expires(self):
        self.assertTrue(alert_mod._should_alert('A', 600, 1000.0))
        self.assertTrue(alert_mod._should_alert('A', 600, 1700.0))

    def test_cooldown_is_per_nickname(self):
        self.assertTrue(alert_mod._should_alert('A', 600, 1000.0))
        self.assertTrue(alert_mod._should_alert('B', 600, 1000.0))

    def test_alert_never_raises_even_if_everything_broken(self):
        """告警链路挂了也绝不能反过来搞垮监听链路。"""
        original = alert_mod.load
        alert_mod.load = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            self.assertFalse(alert_mod.alert_listen_failure(None, 'X'))
        finally:
            alert_mod.load = original

    def test_disabled_config_skips(self):
        original = alert_mod.load
        alert_mod.load = lambda: {'alert': {'enabled': False}}
        try:
            self.assertFalse(alert_mod.alert_listen_failure(FakeBot(), 'X'))
        finally:
            alert_mod.load = original


class TestProbe(unittest.TestCase):

    def setUp(self):
        probe_mod._consecutive_fail = 0
        self._rows = []
        self._orig_record = probe_mod._record
        probe_mod._record = lambda row: self._rows.append(row)

    def tearDown(self):
        probe_mod._record = self._orig_record

    def _cfg(self, **over):
        base = {'probe': {'enabled': True, 'target': '文件传输助手',
                          'alert_after_consecutive': 3}}
        base['probe'].update(over)
        return base

    def test_success_path_records_ok(self):
        bot = FakeBot(FakeWx())
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(bot)
        self.assertEqual(len(self._rows), 1)
        self.assertTrue(self._rows[0]['ok'])
        self.assertIsNone(self._rows[0]['err'])

    def test_failure_path_records_error(self):
        bot = FakeBot(FakeWx(add_raises=OSError("error(1400, 'MoveWindow', '无效的窗口句柄。')")))
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(bot)
        self.assertFalse(self._rows[0]['ok'])
        self.assertIn('1400', self._rows[0]['err'])

    def test_subwindow_none_counts_as_failure(self):
        """AddListenChat 不抛异常但拿不到子窗口，同样算失败——这正是线上那个坑。"""
        bot = FakeBot(FakeWx(sub=None))
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(bot)
        self.assertFalse(self._rows[0]['ok'])

    def test_always_cleans_up_listener(self):
        wx = FakeWx()
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(FakeBot(wx))
        self.assertEqual(wx.removed, ['文件传输助手'])

    def test_skips_when_target_already_listened(self):
        """目标已被正式监听时不能去动它的窗口。"""
        class Sub:
            who = '文件传输助手'
        wx = FakeWx(subwindows=[Sub()])
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(FakeBot(wx))
        self.assertEqual(self._rows, [])
        self.assertEqual(wx.removed, [])

    def test_disabled_does_nothing(self):
        wx = FakeWx()
        probe_mod.load = lambda: self._cfg(enabled=False)
        probe_mod.tick(FakeBot(wx))
        self.assertEqual(self._rows, [])
        self.assertEqual(wx.removed, [])

    def test_no_wx_does_nothing(self):
        bot = FakeBot()
        bot.wx = None
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(bot)
        self.assertEqual(self._rows, [])

    def test_consecutive_failures_trigger_alert_once(self):
        calls = []
        alert_mod._last_alert.clear()
        orig = alert_mod.alert_listen_failure
        import plugins.listen_health.alert as a
        a.alert_listen_failure = lambda *args, **kw: calls.append(args) or True
        probe_mod.load = lambda: self._cfg(alert_after_consecutive=2)
        try:
            bot = FakeBot(FakeWx(add_raises=OSError('boom')))
            for _ in range(4):
                probe_mod.tick(bot)
            # 只在恰好第 2 次时告警一次，之后不再刷
            self.assertEqual(len(calls), 1)
        finally:
            a.alert_listen_failure = orig

    def test_success_resets_consecutive_counter(self):
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(FakeBot(FakeWx(add_raises=OSError('x'))))
        self.assertEqual(probe_mod._consecutive_fail, 1)
        probe_mod.tick(FakeBot(FakeWx()))
        self.assertEqual(probe_mod._consecutive_fail, 0)

    def test_tick_swallows_unexpected_errors(self):
        """探针再怎么炸都不能冒泡到 schedule 主循环。"""
        probe_mod.load = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        probe_mod.tick(FakeBot())  # 不抛就算过


class TestRegister(unittest.TestCase):

    class FakeSchedule:
        def __init__(self):
            self.cleared = []
            self.tagged = []

        def clear(self, tag):
            self.cleared.append(tag)

        def every(self, n):
            outer = self

            class J:
                def __init__(self):
                    self.n = n

                @property
                def minutes(self):
                    return self

                def do(self, fn, bot):
                    return self

                def tag(self, t):
                    outer.tagged.append((n, t))
                    return self
            return J()

    def test_register_sets_flag_and_tag(self):
        sch = self.FakeSchedule()
        bot = FakeBot()
        probe_mod.load = lambda: {'probe': {'enabled': True, 'interval_min': 10,
                                            'target': '文件传输助手'}}
        probe_mod.register(bot, sch)
        self.assertTrue(getattr(bot, '_listen_probe_enabled', False))
        self.assertEqual(sch.tagged, [(10, 'listen_probe')])

    def test_register_skipped_when_disabled(self):
        sch = self.FakeSchedule()
        bot = FakeBot()
        probe_mod.load = lambda: {'probe': {'enabled': False}}
        probe_mod.register(bot, sch)
        self.assertFalse(getattr(bot, '_listen_probe_enabled', False))
        self.assertEqual(sch.tagged, [])


class TestConfig(unittest.TestCase):

    def test_defaults_when_file_missing(self):
        orig = cfg_mod.CONFIG_PATH
        cfg_mod.CONFIG_PATH = os.path.join(tempfile.gettempdir(), 'no-such-listen-health.json')
        try:
            cfg = cfg_mod.load()
            self.assertTrue(cfg['alert']['enabled'])
            self.assertEqual(cfg['probe']['target'], '文件传输助手')
        finally:
            cfg_mod.CONFIG_PATH = orig

    def test_partial_config_merges_with_defaults(self):
        orig = cfg_mod.CONFIG_PATH
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({'probe': {'interval_min': 3}}, f)
            path = f.name
        cfg_mod.CONFIG_PATH = path
        try:
            cfg = cfg_mod.load()
            self.assertEqual(cfg['probe']['interval_min'], 3)
            self.assertEqual(cfg['probe']['target'], '文件传输助手')  # 默认值没被抹掉
            self.assertTrue(cfg['alert']['enabled'])
        finally:
            cfg_mod.CONFIG_PATH = orig
            os.unlink(path)

    def test_local_config_is_valid_json_if_present(self):
        """data/ 不进版本库（默认值在 config.py），本机有这个文件时顺手验一下格式。"""
        if not os.path.exists(cfg_mod.CONFIG_PATH):
            self.skipTest('本机没有 data/config.json，用 DEFAULTS')
        with open(cfg_mod.CONFIG_PATH, encoding='utf-8') as f:
            json.load(f)


class TestBackoffConstant(unittest.TestCase):
    """退避常量在 wxbot_core 里（不能 import，会拉起 wxautox），用 ast 读出来验。"""

    def test_backoff_is_increasing_and_longer_than_before(self):
        with open(os.path.join(ROOT, 'wxbot_core.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == 'RETRY_BACKOFF':
                        found = ast.literal_eval(node.value)
        self.assertIsNotNone(found, 'wxbot_core 里找不到 RETRY_BACKOFF')
        self.assertEqual(list(found), sorted(found), '退避间隔必须递增')
        self.assertGreater(sum(found), 16, '总等待时间要明显长于改造前的 4 次 x 0.5 秒')


if __name__ == '__main__':
    unittest.main(verbosity=2)
