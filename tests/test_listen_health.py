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
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 单测的日志不许写进 panel_logs（生产日志）——必须在导入插件【之前】设。
os.environ.setdefault("NCC_LOG_SILENT", "1")

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
        # auto_restart 必须显式关掉：探针失败用例会把连续失败数推过自愈阈值，
        # 不关的话在 Windows 生产机上跑单测会真的触发 SWXPanelRestart。
        base = {'probe': {'enabled': True, 'target': '文件传输助手',
                          'alert_after_consecutive': 3, 'auto_restart': False}}
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

    def test_failure_calls_heal(self):
        """失败要走自愈判定，别只记个日志。"""
        calls = []
        import plugins.listen_health.heal as h
        orig = h.maybe_heal
        h.maybe_heal = lambda bot, n, cfg, **kw: calls.append(n) or 'none'
        probe_mod.load = lambda: self._cfg()
        try:
            probe_mod.tick(FakeBot(FakeWx(add_raises=OSError('x'))))
        finally:
            h.maybe_heal = orig
        self.assertEqual(calls, [1])

    def test_env_snapshot_recorded(self):
        """环境快照要落到采样行里（非 Windows 上是空 dict，字段本身必须在）。"""
        probe_mod.load = lambda: self._cfg()
        probe_mod.tick(FakeBot(FakeWx()))
        self.assertIn('env', self._rows[0])
        self.assertIsInstance(self._rows[0]['env'], dict)

    def test_env_snapshot_never_raises(self):
        self.assertIsInstance(probe_mod._env_snapshot(), dict)


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


class TestHeal(unittest.TestCase):
    """自愈：连续失败 -> 自动重启；冷却期内再失败 -> 判定重启无效，叫人。"""

    # setUp 替换的模块级名字，tearDown 逐个还回去
    _PATCHED = ('_trigger_restart', '_alert', 'load_state', 'save_state')

    def setUp(self):
        from plugins.listen_health import heal as heal_mod
        self.heal = heal_mod
        self.restarts = []
        self.alerts = []
        self.state = {}
        # 原值先存好再替换 —— 少了这一步，本类跑完后模块里留着一堆假货，
        # 后面任何想测「真」_trigger_restart 的用例都会静默测到 mock 上（踩过）。
        self._orig = {name: getattr(heal_mod, name) for name in self._PATCHED}
        heal_mod._trigger_restart = lambda task: self.restarts.append(task)
        heal_mod._alert = lambda bot, title, content: self.alerts.append((title, content))
        heal_mod.load_state = lambda: dict(self.state)
        heal_mod.save_state = lambda s: self.state.update(s)

    def tearDown(self):
        for name, orig in self._orig.items():
            setattr(self.heal, name, orig)

    def _cfg(self, **over):
        cfg = {'auto_restart': True, 'restart_after_consecutive': 2,
               'restart_cooldown_min': 60, 'restart_task_name': 'SWXPanelRestart'}
        cfg.update(over)
        return cfg

    def test_below_threshold_does_nothing(self):
        self.assertEqual(self.heal.maybe_heal(None, 1, self._cfg()), 'none')
        self.assertEqual(self.restarts, [])

    def test_threshold_triggers_restart(self):
        self.assertEqual(self.heal.maybe_heal(None, 2, self._cfg()), 'restart')
        self.assertEqual(self.restarts, ['SWXPanelRestart'])
        self.assertEqual(len(self.alerts), 1)

    def test_state_persisted_before_restart(self):
        """必须先落盘再重启——进程被杀掉后内存状态就没了。"""
        self.heal.maybe_heal(None, 2, self._cfg())
        self.assertGreater(self.state.get('last_restart_ts', 0), 0)
        self.assertEqual(self.state.get('restart_count'), 1)

    def test_cooldown_blocks_second_restart_and_escalates(self):
        import time as _t
        self.state = {'last_restart_ts': _t.time() - 60, 'escalated': False}
        self.assertEqual(self.heal.maybe_heal(None, 2, self._cfg()), 'give_up')
        self.assertEqual(self.restarts, [], '冷却期内绝不能再重启')
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('人工', self.alerts[0][0])

    def test_escalation_alert_sent_only_once(self):
        import time as _t
        self.state = {'last_restart_ts': _t.time() - 60, 'escalated': False}
        for _ in range(4):
            self.heal.maybe_heal(None, 3, self._cfg())
        self.assertEqual(len(self.alerts), 1, '升级告警不能每轮都刷')

    def test_restart_allowed_after_cooldown_expires(self):
        import time as _t
        self.state = {'last_restart_ts': _t.time() - 3601, 'escalated': True}
        self.assertEqual(self.heal.maybe_heal(None, 2, self._cfg()), 'restart')
        self.assertEqual(self.restarts, ['SWXPanelRestart'])
        self.assertFalse(self.state['escalated'], '新一轮自愈要清掉升级标记')

    def test_disabled_never_restarts(self):
        self.assertEqual(self.heal.maybe_heal(None, 9, self._cfg(auto_restart=False)), 'none')
        self.assertEqual(self.restarts, [])

    def test_never_raises_when_trigger_broken(self):
        self.heal._trigger_restart = lambda task: (_ for _ in ()).throw(OSError('schtasks not found'))
        self.assertEqual(self.heal.maybe_heal(None, 2, self._cfg()), 'none')


class TestHealAlertHonorsChannelSwitches(unittest.TestCase):
    """★ 回归：自愈通知必须读 alert.webhook / alert.admin_group 这两个开关。

    原来 heal._alert 把两个通道写死成"都发"，于是把 admin_group 关掉（约定是运维状态
    消息只发飞书）之后，探针告警确实不发微信了，而「监听进入坏状态，即将自动重启
    机器人」照旧往管理群里灌 —— 开关只生效了一半，人只会以为自己没关掉。
    这里【测真的 _alert】，所以不能放进 TestHeal（那个类把 _alert 整个换成了 mock）。
    """

    def setUp(self):
        from plugins.listen_health import heal as heal_mod
        from plugins.ncc_community import common as ncc_common
        self.heal, self.ncc_common = heal_mod, ncc_common
        self.webhooks, self.admins = [], []

        fake_webhook = types.ModuleType('webhook_send')
        fake_webhook.send_message = lambda t, c: (self.webhooks.append((t, c)), (True, ''))[1]
        self._orig_module = sys.modules.get('webhook_send')
        sys.modules['webhook_send'] = fake_webhook

        self._orig_notify = ncc_common.notify_admin
        ncc_common.notify_admin = lambda bot, cfg, text: self.admins.append(text)

        self._orig_path = cfg_mod.CONFIG_PATH

    def tearDown(self):
        if self._orig_module is None:
            sys.modules.pop('webhook_send', None)
        else:
            sys.modules['webhook_send'] = self._orig_module
        self.ncc_common.notify_admin = self._orig_notify
        cfg_mod.CONFIG_PATH = self._orig_path

    def _with_alert_cfg(self, **alert):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({'alert': alert}, f)
            cfg_mod.CONFIG_PATH = f.name
        self.addCleanup(os.unlink, f.name)

    def test_admin_group_off_means_feishu_only(self):
        self._with_alert_cfg(webhook=True, admin_group=False)
        self.heal._alert(None, '监听进入坏状态', '即将自动重启机器人')
        self.assertEqual(len(self.webhooks), 1)          # 飞书照发
        self.assertEqual(self.admins, [])                # 微信管理群一条都不发

    def test_both_on_still_sends_both(self):
        self._with_alert_cfg(webhook=True, admin_group=True)
        self.heal._alert(None, '标题', '正文')
        self.assertEqual(len(self.webhooks), 1)
        self.assertEqual(len(self.admins), 1)

    def test_webhook_off_means_admin_group_only(self):
        self._with_alert_cfg(webhook=False, admin_group=True)
        self.heal._alert(None, '标题', '正文')
        self.assertEqual(self.webhooks, [])
        self.assertEqual(len(self.admins), 1)

    def test_local_config_actually_routes_status_to_feishu_only(self):
        """本机配置的实际效果：状态消息只进飞书。改回去会在这里失败。"""
        if not os.path.exists(self._orig_path):
            self.skipTest('本机没有 data/config.json，用 DEFAULTS')
        cfg_mod.CONFIG_PATH = self._orig_path
        self.heal._alert(None, '监听进入坏状态', '即将自动重启机器人')
        self.assertEqual(self.admins, [], '运维状态消息不该发进微信管理群')


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


class TestTriggerRestartWritesAutostartFlag(unittest.TestCase):
    """★ 自愈重启必须连自启动标记一起写，否则面板起来了、机器人是停的。

    2026-08-11 真出过事：_trigger_restart 只复用了 ui_watchdog 的触发那一半、
    漏了写标记，机器人从 16:05 一直下线到人发现（1.5 小时）。
    注意 TestHeal 整个把 _trigger_restart 换成了 mock，所以那批用例天然测不到
    它内部干了什么 —— 这里必须调真正的 _trigger_restart，只替掉它下游的
    _default_trigger（不然会真去跑 schtasks）。
    """

    def setUp(self):
        from plugins.listen_health import heal as heal_mod
        from plugins import ui_watchdog as wd_mod
        self.heal_mod = heal_mod
        self.wd_mod = wd_mod
        self.calls = []

        self._orig_trigger = wd_mod._default_trigger
        self._orig_flag_file = wd_mod._FLAG_FILE
        self._orig_data_dir = wd_mod._DATA_DIR

        self.tmp = tempfile.mkdtemp()
        wd_mod._DATA_DIR = self.tmp
        wd_mod._FLAG_FILE = os.path.join(self.tmp, 'autostart.flag')
        wd_mod._default_trigger = lambda task: self.calls.append(('trigger', task))

    def tearDown(self):
        self.wd_mod._default_trigger = self._orig_trigger
        self.wd_mod._FLAG_FILE = self._orig_flag_file
        self.wd_mod._DATA_DIR = self._orig_data_dir

    def test_flag_written_and_restart_triggered(self):
        self.heal_mod._trigger_restart('SWXPanelRestart')

        self.assertTrue(os.path.exists(self.wd_mod._FLAG_FILE),
                        '触发了重启却没写自启动标记 —— 机器人重启后不会被拉起')
        self.assertEqual([('trigger', 'SWXPanelRestart')], self.calls)

    def test_flag_is_fresh_enough_to_be_consumed(self):
        """标记写完要能被 consume_autostart_flag 认可，否则等于没写。"""
        self.heal_mod._trigger_restart('SWXPanelRestart')
        self.assertTrue(self.wd_mod.consume_autostart_flag(flag_valid_seconds=600))

    def test_flag_written_before_trigger(self):
        """顺序不能反：先写标记再触发重启，否则可能进程已被杀、标记还没落盘。"""
        order = []
        self.wd_mod._default_trigger = lambda task: order.append(
            'trigger:flag_exists=%s' % os.path.exists(self.wd_mod._FLAG_FILE))
        self.heal_mod._trigger_restart('SWXPanelRestart')
        self.assertEqual(['trigger:flag_exists=True'], order)


if __name__ == '__main__':
    unittest.main(verbosity=2)
