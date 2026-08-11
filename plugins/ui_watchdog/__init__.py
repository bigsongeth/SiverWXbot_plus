# -*- coding: utf-8 -*-
"""
UI 看门狗插件：检测 wxautox UI 自动化异常并自动整进程重启。两种检测：

1. 主循环心跳停滞（stall_seconds，默认 300 秒）——
   wxautox4 的 AddListenChat 等 UI 操作带全局 @uilock（threading.RLock），
   一旦内部循环卡死（如监听子窗口弹不出来），锁永远不会释放，同进程内所有微信
   操作都会永久阻塞，Python 层无法杀线程也无法强制解锁，唯一出路是整进程重启。

2. wxauto 日志「消息解析失败」连续出现（2026-07-30 加）——
   RDP 会话断开/重连后微信 UIA 层异常时，主循环活着、每 3 秒正常轮询（心跳
   正常，检测 1 抓不到），但每条真实消息的发送者属性解析都失败
   （chatbox.py:743「消息解析失败」），消息被静默丢弃，机器人表现为"卡住不
   回复"。机器人线程重启无效，同样只能整进程重启。

重启走 SWXPanelRestart 计划任务（只杀 web_server 的 python，不碰微信进程）。

hook 共 3 处（业务逻辑全在本插件，核心文件只加最小改动）：
- wxbot_core.py 主循环每轮调用 heartbeat()（首次调用自动武装看门狗）
- wxbot_core.py 主循环正常退出后调用 disarm()（停止机器人不算卡死）
- web_server.py 启动时调用 consume_autostart_flag()，为真则自动拉起机器人
（日志检测跑在看门狗自己的后台线程里，不需要额外 hook。）

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
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_DIR))
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
    # —— 日志「消息解析失败」检测（2026-07-30 RDP 断连致 UIA 解析失败，心跳检测抓不到）——
    'log_check_enabled': True,
    # wxauto 日志目录，空串 = 项目根下的 wxauto_logs
    'wxauto_log_dir': '',
    # 窗口期内累计多少条「消息解析失败」触发重启（期间有成功解析消息则清零）
    'parse_fail_threshold': 3,
    'parse_fail_window_seconds': 600,
    # 触发一次日志重启后的冷却期（秒）：重启生效前主循环还活着、还在产失败日志，
    # 没有冷却会在 SWXPanelRestart 生效前重复触发
    'log_fire_cooldown_seconds': 300,
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


class LogParseFailMonitor:
    """
    增量 tail wxauto_logs/app_YYYYMMDD.log，检测「消息解析失败」连续出现。

    判定：窗口期内累计 ≥threshold 条「消息解析失败」，且期间没有成功解析的
    [friend]/[self] 消息（成功一条即清零计数）。首次打开日志从文件末尾开始
    tail，进程启动前的历史日志不参与判定；跨天自动切到新文件。
    """

    def __init__(self, log_dir, window_seconds, threshold, now=time.time,
                 fail_pattern='消息解析失败',
                 success_patterns=('[friend]获取到新消息', '[self]获取到新消息')):
        self.log_dir = log_dir
        self.window_seconds = window_seconds
        self.threshold = threshold
        self._now = now
        self.fail_pattern = fail_pattern
        self.success_patterns = tuple(success_patterns)
        self._cur_file = None
        self._pos = 0
        self._fail_times = []  # 每条失败日志被观察到的时间（本进程时钟）

    def _log_path(self):
        """优先跟踪目录里最新修改的 app_*.log。

        wxautox 的日志文件名取自进程启动日，跨天后仍写旧文件名（实测 2026-07-31
        00:08 还在写 app_20260730.log）——按当天日期拼文件名会在午夜后盯错文件，
        检测变瞎。目录里还没有任何日志时才退回按当天日期拼。"""
        try:
            candidates = [os.path.join(self.log_dir, n) for n in os.listdir(self.log_dir)
                          if n.startswith('app_') and n.endswith('.log')]
            if candidates:
                return max(candidates, key=os.path.getmtime)
        except OSError:
            pass
        day = time.strftime('%Y%m%d', time.localtime(self._now()))
        return os.path.join(self.log_dir, f'app_{day}.log')

    def scan(self):
        """读取新增日志行并更新计数，达到重启阈值返回 True。"""
        path = self._log_path()
        if path != self._cur_file:
            if self._cur_file is None and os.path.exists(path):
                # 首次打开：跳到末尾只看之后的新增，避免历史失败日志误触发
                self._pos = os.path.getsize(path)
            else:
                # 跨天新文件（或首次打开时文件还不存在）：内容全是新的，从头读
                self._pos = 0
            self._cur_file = path
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size < self._pos:
                self._pos = 0  # 文件被截断/重写
            if size > self._pos:
                with open(path, 'rb') as f:
                    f.seek(self._pos)
                    chunk = f.read()
                # 只处理完整行，末尾未写完的半行留到下次
                cut = chunk.rfind(b'\n')
                if cut >= 0:
                    for line in chunk[:cut].decode('utf-8', errors='replace').splitlines():
                        self._feed_line(line)
                    self._pos += cut + 1
        cutoff = self._now() - self.window_seconds
        self._fail_times = [t for t in self._fail_times if t >= cutoff]
        return len(self._fail_times) >= self.threshold

    def _feed_line(self, line):
        if any(p in line for p in self.success_patterns):
            self._fail_times = []  # 有消息成功解析，UIA 层没坏，清零
        elif self.fail_pattern in line:
            self._fail_times.append(self._now())

    def fail_count(self):
        return len(self._fail_times)

    def reset(self):
        self._fail_times = []


class UIWatchdog:
    """
    主循环心跳看门狗。

    now / trigger / notify 可注入，便于单测；start_thread=False 时不起后台线程，
    由测试代码直接调用 check_once()。
    """

    def __init__(self, config=None, now=time.time, trigger=None, notify=None,
                 start_thread=True):
        if config is not None:
            self.cfg = dict(_DEFAULTS)
            self.cfg.update(config)
        else:
            self.cfg = _load_config()
        self._now = now
        self._trigger = trigger if trigger is not None else _default_trigger
        self._notify = notify if notify is not None else _default_notify
        self._start_thread = start_thread
        self._lock = threading.Lock()
        self._armed = False
        self._fired = False
        self._last_beat = None
        self._thread = None
        log_dir = self.cfg['wxauto_log_dir'] or os.path.join(_PROJECT_ROOT, 'wxauto_logs')
        self._log_monitor = LogParseFailMonitor(
            log_dir,
            self.cfg['parse_fail_window_seconds'],
            self.cfg['parse_fail_threshold'],
            now=now)
        self._last_log_fire = None

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
        """执行一次检查（心跳停滞 + 日志解析失败），触发了重启返回 True。后台线程和单测共用。"""
        with self._lock:
            if not self.cfg.get('enabled', True):
                return False
            armed = self._armed
            fire_stall = False
            stalled = 0
            if armed and not self._fired and self._last_beat is not None:
                stalled = self._now() - self._last_beat
                if stalled >= self.cfg['stall_seconds']:
                    # 先置位再动作，防止重启生效前重复触发
                    self._fired = True
                    fire_stall = True

        if fire_stall:
            return self._fire_restart(
                f'主循环心跳已停滞 {int(stalled)} 秒，判定 wxautox UI '
                '自动化卡死（多为监听子窗口弹出失败）',
                f'主循环卡死 {int(stalled)} 秒')
        return self._check_log_once(armed)

    # ---- 日志「消息解析失败」检测 ----

    def _check_log_once(self, armed):
        """扫一轮 wxauto 日志：解析失败达到阈值则触发同一条重启链路。"""
        if not armed or not self.cfg['log_check_enabled']:
            return False
        try:
            hit = self._log_monitor.scan()
        except Exception as e:
            _log('ERROR', f'【看门狗】扫描 wxauto 日志出错: {e}')
            return False
        if not hit:
            return False
        now = self._now()
        if (self._last_log_fire is not None
                and now - self._last_log_fire < self.cfg['log_fire_cooldown_seconds']):
            return False
        self._last_log_fire = now
        fail_count = self._log_monitor.fail_count()
        window_min = int(self.cfg['parse_fail_window_seconds'] / 60)
        fired = self._fire_restart(
            f'wxauto 日志 {window_min} 分钟内出现 {fail_count} 条「消息解析失败」且期间'
            '无成功解析的消息，判定微信 UIA 层异常（多为 RDP 会话断开/重连所致），'
            '消息正被静默丢弃',
            f'消息解析连续失败 {fail_count} 次')
        self._log_monitor.reset()
        return fired

    # ---- 重启链路（两种检测共用） ----

    def _fire_restart(self, detail, brief):
        """detail 进日志（含判定依据），brief 进 webhook 通知。"""
        if not self._allow_restart():
            _log('ERROR', f'【看门狗】{detail}，但最近一小时'
                          f'自动重启已达 {self.cfg["max_restarts_per_hour"]} 次上限，'
                          '不再重启，请人工检查微信窗口状态')
            self._notify_best_effort(
                'wxbot 看门狗告警',
                f'{brief}，自动重启次数超限已放弃，请人工处理')
            return False

        _log('ERROR', f'【看门狗】{detail}，正在触发整进程重启...')
        try:
            # 写自启动标记（失败不阻止重启）
            flag_ok = self._write_autostart_flag()
            self._record_restart()
            self._notify_best_effort(
                'wxbot 看门狗自动重启',
                f'{brief}，已触发面板整进程重启并将自动拉起机器人')
            self._trigger(self.cfg['restart_task_name'])
            _log('WARNING', f'【看门狗】已触发计划任务 {self.cfg["restart_task_name"]}，'
                            '等待进程被重启...')
            if not flag_ok:
                _log('WARNING', f'【看门狗】自启动标记写入失败，但重启继续执行；web_server 将使用备用自启机制')
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
        """记录重启时间，带错误处理。"""
        try:
            history = [t for t in self._load_history() if self._now() - t < 3600]
            history.append(self._now())
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f)
            _log('INFO', f'【看门狗】重启历史已记录: {_HISTORY_FILE}')
        except Exception as e:
            _log('ERROR', f'【看门狗】记录重启历史失败: {e}')

    def _write_autostart_flag(self):
        """写入自启动标记文件（实现见模块级 write_autostart_flag，两边共用一份）。"""
        return write_autostart_flag('ui_stall_restart', self._now)

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


def write_autostart_flag(reason='restart', now=time.time):
    """写入自启动标记：整进程重启后 web_server 读到它会自动拉起机器人。

    ★ 任何触发 SWXPanelRestart 的调用方都必须先调这个。只触发重启而不写标记，
    面板会起来、机器人却是停的 —— 那比不自愈更糟：本来只是监听坏了（丢部分消息），
    自愈之后变成机器人整个下线（丢全部消息），还得等人发现。
    2026-08-11 listen_health 首次真触发自愈时就是这样，机器人下线 1.5 小时无人知。
    """
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
    except Exception as e:
        _log('ERROR', f'【自启动标记】创建数据目录失败: {e}')
        return False

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with open(_FLAG_FILE, 'w', encoding='utf-8') as f:
                json.dump({'ts': now(), 'reason': reason}, f)
            _log('INFO', f'【自启动标记】已写入（{reason}）: {_FLAG_FILE}')
            return True
        except Exception as e:
            _log('WARNING', f'【自启动标记】写入失败（第 {attempt+1} 次尝试）: {e}')
            if attempt < max_retries - 1:
                time.sleep(0.5)

    _log('ERROR', '【自启动标记】写入最终失败，已耗尽重试次数')
    return False


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
