# -*- coding: utf-8 -*-
"""ui_watchdog 插件单测：纯 mock，不碰微信、不起后台线程、不跑真实计划任务。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.ui_watchdog as wd
from plugins.ui_watchdog import UIWatchdog, consume_autostart_flag


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_watchdog(clock, tmp_files, **cfg_overrides):
    """构造不起后台线程、trigger/notify 全 mock 的看门狗。"""
    cfg = {
        'enabled': True,
        'stall_seconds': 300,
        'check_interval': 30,
        'restart_task_name': 'SWXPanelRestart',
        'max_restarts_per_hour': 3,
        'flag_valid_seconds': 600,
    }
    cfg.update(cfg_overrides)
    triggered = []
    notified = []
    dog = UIWatchdog(
        config=cfg,
        now=clock,
        trigger=lambda task: triggered.append(task),
        notify=lambda title, content: notified.append((title, content)),
        start_thread=False,
    )
    return dog, triggered, notified


class UIWatchdogTest(unittest.TestCase):
    def setUp(self):
        # 隔离运行时文件，避免污染真实 data 目录
        self._orig_data_dir = wd._DATA_DIR
        self._orig_flag = wd._FLAG_FILE
        self._orig_history = wd._HISTORY_FILE
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='ui_watchdog_test_')
        wd._DATA_DIR = self._tmp
        wd._FLAG_FILE = os.path.join(self._tmp, 'autostart.flag')
        wd._HISTORY_FILE = os.path.join(self._tmp, 'restart_history.json')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        wd._DATA_DIR = self._orig_data_dir
        wd._FLAG_FILE = self._orig_flag
        wd._HISTORY_FILE = self._orig_history

    # ---- 基本触发逻辑 ----

    def test_no_fire_before_armed(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        clock.advance(10000)
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_no_fire_within_threshold(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        clock.advance(299)
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_fire_after_stall(self):
        clock = FakeClock()
        dog, triggered, notified = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        clock.advance(301)
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart'])
        self.assertTrue(os.path.exists(wd._FLAG_FILE))
        self.assertEqual(len(notified), 1)

    def test_fire_only_once_until_next_beat(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        clock.advance(301)
        self.assertTrue(dog.check_once())
        clock.advance(100)
        self.assertFalse(dog.check_once())  # 已触发过，重启生效前不重复触发
        dog.heartbeat()  # 恢复心跳后重置触发态
        clock.advance(301)
        self.assertTrue(dog.check_once())
        self.assertEqual(len(triggered), 2)

    def test_no_fire_when_disarmed(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        dog.disarm()  # 正常停止机器人
        clock.advance(10000)
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_no_fire_when_disabled(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp, enabled=False)
        dog.heartbeat()
        clock.advance(10000)
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_heartbeat_keeps_alive(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        for _ in range(10):
            dog.heartbeat()
            clock.advance(200)  # 每 200 秒一次心跳，永不超过 300 秒阈值
            self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    # ---- 重启风暴保护 ----

    def test_rate_limit_blocks_fourth_restart(self):
        clock = FakeClock()
        dog, triggered, notified = make_watchdog(clock, self._tmp)
        for i in range(4):
            dog.heartbeat()
            clock.advance(301)
            fired = dog.check_once()
            if i < 3:
                self.assertTrue(fired, f'第 {i + 1} 次应触发重启')
            else:
                self.assertFalse(fired, '第 4 次应被限流')
        self.assertEqual(len(triggered), 3)
        # 第 4 次仍应发告警通知
        self.assertEqual(len(notified), 4)

    def test_rate_limit_window_expires(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        for _ in range(3):
            dog.heartbeat()
            clock.advance(301)
            self.assertTrue(dog.check_once())
        clock.advance(3700)  # 一小时窗口滑过
        dog.heartbeat()
        clock.advance(301)
        self.assertTrue(dog.check_once())
        self.assertEqual(len(triggered), 4)

    # ---- 自启动标记 ----

    def test_consume_fresh_flag(self):
        clock = FakeClock()
        dog, _, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        clock.advance(301)
        dog.check_once()
        clock.advance(60)
        self.assertTrue(consume_autostart_flag(now=clock, flag_valid_seconds=600))
        self.assertFalse(os.path.exists(wd._FLAG_FILE))  # 消费后删除
        self.assertFalse(consume_autostart_flag(now=clock, flag_valid_seconds=600))

    def test_stale_flag_ignored(self):
        clock = FakeClock()
        dog, _, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        clock.advance(301)
        dog.check_once()
        clock.advance(9999)  # 标记已过期
        self.assertFalse(consume_autostart_flag(now=clock, flag_valid_seconds=600))
        self.assertFalse(os.path.exists(wd._FLAG_FILE))

    def test_corrupt_flag_ignored(self):
        os.makedirs(wd._DATA_DIR, exist_ok=True)
        with open(wd._FLAG_FILE, 'w', encoding='utf-8') as f:
            f.write('not json')
        clock = FakeClock()
        self.assertFalse(consume_autostart_flag(now=clock, flag_valid_seconds=600))
        self.assertFalse(os.path.exists(wd._FLAG_FILE))

    def test_no_flag_returns_false(self):
        self.assertFalse(consume_autostart_flag(now=FakeClock(), flag_valid_seconds=600))

    # ---- 触发失败兜底 ----

    def test_trigger_failure_returns_false(self):
        clock = FakeClock()

        def bad_trigger(task):
            raise OSError('schtasks not found')

        cfg = {
            'enabled': True, 'stall_seconds': 300, 'check_interval': 30,
            'restart_task_name': 'SWXPanelRestart',
            'max_restarts_per_hour': 3, 'flag_valid_seconds': 600,
        }
        dog = UIWatchdog(config=cfg, now=clock, trigger=bad_trigger,
                         notify=lambda t, c: None, start_thread=False)
        dog.heartbeat()
        clock.advance(301)
        self.assertFalse(dog.check_once())

    # ---- schtasks 定位（2026-07-30 03:20 真实哑火：schtasks not found） ----

    def test_schtasks_exe_uses_absolute_path_on_windows(self):
        """计划任务/服务上下文的 PATH 可能没有 System32，必须走 %SystemRoot% 绝对路径。"""
        import tempfile
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix='wd_sysroot_')
        sys32 = os.path.join(tmp, 'System32')
        os.makedirs(sys32, exist_ok=True)
        exe = os.path.join(sys32, 'schtasks.exe')
        open(exe, 'w').close()
        with mock.patch.object(wd.sys, 'platform', 'win32'), \
             mock.patch.dict(os.environ, {'SystemRoot': tmp}):
            self.assertEqual(wd._schtasks_exe(), exe)

    def test_schtasks_exe_falls_back_to_bare_name(self):
        from unittest import mock
        with mock.patch.object(wd.sys, 'platform', 'linux'):
            self.assertEqual(wd._schtasks_exe(), 'schtasks')


if __name__ == '__main__':
    unittest.main()
