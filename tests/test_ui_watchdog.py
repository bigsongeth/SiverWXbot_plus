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
    """构造不起后台线程、trigger/notify 全 mock 的看门狗（日志目录指向临时目录）。"""
    cfg = {
        'enabled': True,
        'stall_seconds': 300,
        'check_interval': 30,
        'restart_task_name': 'SWXPanelRestart',
        'max_restarts_per_hour': 3,
        'flag_valid_seconds': 600,
        'wxauto_log_dir': tmp_files,
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

    # ---- 日志「消息解析失败」检测（2026-07-30 RDP 断连故障，心跳检测抓不到） ----

    FAIL_LINE = ('2026-07-30 14:25:35 [wxautox4(40.1.15)] [DEBUG] [chatbox.py:743]  '
                 '[True|56]消息解析失败（失败1次，连续失败1次）')
    OK_LINE = ('2026-07-30 04:38:07 [wxautox4(40.1.15)] [DEBUG] [wx.py:385]  '
               '[friend]获取到新消息：松爸 - 早')

    def _append_log(self, dog, lines, newline_at_end=True):
        """往看门狗当前监控的日志文件追加行。"""
        path = dog._log_monitor._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            for i, line in enumerate(lines):
                last = (i == len(lines) - 1)
                f.write(line + ('' if last and not newline_at_end else '\n'))
        return path

    def test_log_parse_fail_triggers_restart(self):
        clock = FakeClock()
        dog, triggered, notified = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        self.assertFalse(dog.check_once())  # 注册日志文件，尚无内容
        self._append_log(dog, [self.FAIL_LINE] * 3)
        clock.advance(30)
        dog.heartbeat()
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart'])
        self.assertTrue(os.path.exists(wd._FLAG_FILE))  # 重启后要自动拉起机器人
        self.assertIn('消息解析连续失败 3 次', notified[0][1])

    def test_log_success_line_resets_counter(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        # 失败×2 → 成功一条 → 失败×2：期间有成功解析，不算 UIA 层坏死
        self._append_log(dog, [self.FAIL_LINE] * 2 + [self.OK_LINE] + [self.FAIL_LINE] * 2)
        clock.advance(30)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])
        # 再来一条失败凑满 3 条连续失败，应触发
        self._append_log(dog, [self.FAIL_LINE])
        clock.advance(30)
        dog.heartbeat()
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart'])

    def test_log_backlog_ignored_on_first_scan(self):
        """进程启动前日志里的历史失败不算数（首次打开从文件末尾 tail）。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        self._append_log(dog, [self.FAIL_LINE] * 5)  # 启动前已有的失败日志
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        clock.advance(30)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_log_window_expiry(self):
        """失败超出窗口期（600 秒）后不再计入。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self._append_log(dog, [self.FAIL_LINE] * 2)
        clock.advance(30)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        clock.advance(700)  # 前两条滑出窗口
        dog.heartbeat()
        self._append_log(dog, [self.FAIL_LINE])
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_log_fire_cooldown(self):
        """触发后冷却期内不重复触发（重启生效前主循环还在产失败日志）。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self._append_log(dog, [self.FAIL_LINE] * 3)
        clock.advance(30)
        dog.heartbeat()
        self.assertTrue(dog.check_once())
        # 冷却期内又攒了 3 条失败：不触发
        self._append_log(dog, [self.FAIL_LINE] * 3)
        clock.advance(60)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        # 冷却期过后仍在失败：再次触发
        clock.advance(300)
        dog.heartbeat()
        self._append_log(dog, [self.FAIL_LINE] * 3)
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart', 'SWXPanelRestart'])

    def test_log_check_disabled(self):
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp, log_check_enabled=False)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self._append_log(dog, [self.FAIL_LINE] * 5)
        clock.advance(30)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_log_no_fire_when_not_armed(self):
        """机器人没在跑（未武装）时不做日志检测。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        self._append_log(dog, [self.FAIL_LINE] * 5)
        self.assertFalse(dog.check_once())
        self.assertEqual(triggered, [])

    def test_log_partial_line_not_counted(self):
        """未写完的半行（无换行符）留到下次扫描，不提前计数。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        self.assertFalse(dog.check_once())
        self._append_log(dog, [self.FAIL_LINE] * 3, newline_at_end=False)
        clock.advance(30)
        dog.heartbeat()
        self.assertFalse(dog.check_once())  # 只有 2 条完整行
        self._append_log(dog, [''])  # 补上换行，第 3 条完整了
        clock.advance(30)
        dog.heartbeat()
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart'])

    def test_log_follows_newest_file_across_midnight(self):
        """wxautox 日志文件名取自进程启动日，跨天后仍写旧文件；监控按 mtime 跟最新文件。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        path = self._append_log(dog, [self.OK_LINE])  # "昨天"的文件已存在
        self.assertFalse(dog.check_once())            # 注册并跳到文件末尾
        clock.advance(86400 * 2)                      # 日期已变，wxautox 仍写同一个文件
        dog.heartbeat()
        with open(path, 'a', encoding='utf-8') as f:
            for _ in range(3):
                f.write(self.FAIL_LINE + '\n')
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart'])

    def test_log_day_rollover(self):
        """跨天后自动切到新日志文件，从头读。"""
        clock = FakeClock()
        dog, triggered, _ = make_watchdog(clock, self._tmp)
        dog.heartbeat()
        old_path = dog._log_monitor._log_path()
        self.assertFalse(dog.check_once())
        clock.advance(86400 * 2)  # 无论时区，日期必然变了
        dog.heartbeat()
        new_path = dog._log_monitor._log_path()
        self.assertNotEqual(old_path, new_path)
        self._append_log(dog, [self.FAIL_LINE] * 3)
        self.assertTrue(dog.check_once())
        self.assertEqual(triggered, ['SWXPanelRestart'])

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
