# -*- coding: utf-8 -*-
"""
UI 看门狗插件：监控机器人主循环心跳，检测 wxautox UI 自动化卡死并自动整进程重启。

背景：wxautox4 的 AddListenChat 等 UI 操作带全局 @uilock（threading.RLock），
一旦内部循环卡死（如监听子窗口弹不出来），锁永远不会释放，同进程内所有微信
操作都会永久阻塞，Python 层无法杀线程也无法强制解锁，唯一出路是整进程重启。
重启走 SWXPanelRestart 计划任务（只杀 web_server 的 python，不碰微信进程）。

hook 共 3 处（业务逻辑全在本插件，核心文件只加最小改动）：
- wxbot_core.py 主循环每轮调用 heartbeat()（首次调用自动武装看门狗）
- wxbot_core.py 主循环正常退出后调用 disarm()（停止机器人不算卡死）
- web_server.py 启动时调用 consume_autostart_flag()，为真则自动拉起机器人

配置：plugins/ui_watchdog/config.json（可选，缺省用 _DEFAULTS）
运行时数据：plugins/ui_watchdog/data/（自启动标记、重启历史），别提交。
"""

import json
import os
import subprocess
import sys
import threading
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_DIR, 'data')
_CONFIG_FILE = os.path.join(_DIR, 'config.json')
_FLAG_FILE = os.path.join(_DATA_DIR, 'autostart.flag')
_HISTORY_FILE = os.path.join(_DATA_DIR, 'restart_history.json')

_DEFAULTS = {
    # 总开关
    'enabled': True,
    # 心跳停滞多少秒判定为 UI 卡死（要大于正常最长的单轮耗时，如长 AI 回复分段发送）
    'stall_seconds': 300,
    # 看门狗后台线程轮询间隔（秒）
    'check_interval': 30,
    # 触发的整进程重启计划任务名
    'restart_task_name': 'SWXPanelRestart',
    # 每小时最多自动重启次数，超出则只报警不重启，防止反复卡死导致重启风暴
    'max_restarts_per_hour': 3,
    # 自启动标记有效期（秒），防止陈旧标记导致误启动
    'flag_valid_seconds': 600,
}


def _log(level, message):
    """优先用项目日志，插件单测环境下退化为打印。"""
    try:
        from logger import log
        log(level, message)
    except Exception:
        print(f'[ui_watchdog][{level}] {message}')


def _load_config():
    cfg = dict(_DEFAULTS)
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
    except Exception as e:
        _log('WARNING', f'【看门狗】读取配置失败，使用默认配置: {e}')
    return cfg


def _schtasks_exe():
    """schtasks 的绝对路径。

    2026-07-30 03:20 看门狗判定主循环卡死要重启，结果哑火在
    `触发重启失败: schtasks not found`——面板/计划任务的进程环境里 PATH 可能没有
    System32，裸名字 Popen 直接 FileNotFoundError。所以走 %SystemRoot% 绝对路径，
    只在文件确实不存在时才退回裸名字（非 Windows 环境/单测）。"""
    if sys.platform != 'win32':
        return 'schtasks'
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    exe = os.path.join(root, 'System32', 'schtasks.exe')
    return exe if os.path.exists(exe) else 'schtasks'


def _default_trigger(task_name):
    """触发计划任务整进程重启（restart_panel.bat 只杀 web_server 的 python）。"""
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen([_schtasks_exe(), '/run', '/tn', task_name], **kwargs)


def _default_notify(title, content):
    """通过 webhook 通知用户（未配置 webhook 时静默失败）。"""
    from webhook_send import send_message
    send_message(title, content)


class UIWatchdog:
    """
    主循环心跳看门狗。

    now / trigger / notify 可注入，便于单测；start_thread=False 时不起后台线程，
    由测试代码直接调用 check_once()。
    """

    def __init__(self, config=None, now=time.time, trigger=None, notify=None,
                 start_thread=True):
        self.cfg = config if config is not None else _load_config()
        self._now = now
        self._trigger = trigger if trigger is not None else _default_trigger
        self._notify = notify if notify is not None else _default_notify
        self._start_thread = start_thread
        self._lock = threading.Lock()
        self._armed = False
        self._fired = False
        self._last_beat = None
        self._thread = None

    # ---- 对外接口 ----

    def heartbeat(self):
        """主循环每轮调用：更新心跳并武装看门狗。有心跳即视为存活，重置触发态。"""
        with self._lock:
            self._last_beat = self._now()
            self._fired = False
            if not self._armed:
                self._armed = True
                _log('INFO', '【看门狗】已武装，主循环心跳监控启动'
                             f'（停滞 {self.cfg["stall_seconds"]} 秒判定卡死）')
            self._ensure_thread()

    def disarm(self):
        """主循环正常退出时调用：解除武装，停止机器人不算卡死。"""
        with self._lock:
            if self._armed:
                self._armed = False
                _log('INFO', '【看门狗】已解除武装')

    def check_once(self):
        """执行一次卡死检查，触发了重启返回 True。后台线程和单测共用。"""
        with self._lock:
            if not self.cfg.get('enabled', True):
                return False
            if not self._armed or self._fired or self._last_beat is None:
                return False
            stalled = self._now() - self._last_beat
            if stalled < self.cfg['stall_seconds']:
                return False
            # 先置位再动作，防止重启生效前重复触发
            self._fired = True

        if not self._allow_restart():
            _log('ERROR', f'【看门狗】主循环心跳已停滞 {int(stalled)} 秒，但最近一小时'
                          f'自动重启已达 {self.cfg["max_restarts_per_hour"]} 次上限，'
                          '不再重启，请人工检查微信窗口状态')
            self._notify_best_effort(
                'wxbot 看门狗告警',
                f'主循环卡死 {int(stalled)} 秒，自动重启次数超限已放弃，请人工处理')
            return False

        _log('ERROR', f'【看门狗】主循环心跳已停滞 {int(stalled)} 秒，判定 wxautox UI '
                      '自动化卡死（多为监听子窗口弹出失败），正在触发整进程重启...')
        try:
            self._write_autostart_flag()
            self._record_restart()
            self._notify_best_effort(
                'wxbot 看门狗自动重启',
                f'主循环卡死 {int(stalled)} 秒，已触发面板整进程重启并将自动拉起机器人')
            self._trigger(self.cfg['restart_task_name'])
            _log('WARNING', f'【看门狗】已触发计划任务 {self.cfg["restart_task_name"]}，'
                            '等待进程被重启...')
            return True
        except Exception as e:
            _log('ERROR', f'【看门狗】触发重启失败: {e}')
            return False

    # ---- 内部实现 ----

    def _ensure_thread(self):
        if not self._start_thread:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name='ui-watchdog')
            self._thread.start()

    def _run_loop(self):
        while True:
            time.sleep(self.cfg['check_interval'])
            try:
                self.check_once()
            except Exception as e:
                _log('ERROR', f'【看门狗】检查出错: {e}')

    def _load_history(self):
        try:
            if os.path.exists(_HISTORY_FILE):
                with open(_HISTORY_FILE, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                if isinstance(history, list):
                    return [t for t in history if isinstance(t, (int, float))]
        except Exception:
            pass
        return []

    def _allow_restart(self):
        recent = [t for t in self._load_history() if self._now() - t < 3600]
        return len(recent) < self.cfg['max_restarts_per_hour']

    def _record_restart(self):
        history = [t for t in self._load_history() if self._now() - t < 3600]
        history.append(self._now())
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f)

    def _write_autostart_flag(self):
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_FLAG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'ts': self._now(), 'reason': 'ui_stall_restart'}, f)

    def _notify_best_effort(self, title, content):
        try:
            self._notify(title, content)
        except Exception:
            pass


# ---- 模块级单例，供 hook 直接调用 ----

_watchdog = None
_singleton_lock = threading.Lock()


def _get_watchdog():
    global _watchdog
    if _watchdog is None:
        with _singleton_lock:
            if _watchdog is None:
                _watchdog = UIWatchdog()
    return _watchdog


def heartbeat():
    _get_watchdog().heartbeat()


def disarm():
    _get_watchdog().disarm()


def consume_autostart_flag(now=time.time, flag_valid_seconds=None):
    """
    web_server 启动时调用：存在有效的自启动标记则删除并返回 True（应自动启动机器人）。
    标记超过有效期视为陈旧，删除并返回 False，防止误启动。
    """
    if not os.path.exists(_FLAG_FILE):
        return False
    valid_seconds = (flag_valid_seconds if flag_valid_seconds is not None
                     else _load_config()['flag_valid_seconds'])
    fresh = False
    try:
        with open(_FLAG_FILE, 'r', encoding='utf-8') as f:
            flag = json.load(f)
        fresh = now() - flag.get('ts', 0) < valid_seconds
    except Exception:
        fresh = False
    try:
        os.remove(_FLAG_FILE)
    except OSError:
        pass
    if not fresh:
        _log('WARNING', '【看门狗】发现过期的自启动标记，已忽略并清理')
    return fresh
