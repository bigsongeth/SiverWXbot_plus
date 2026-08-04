# -*- coding: utf-8 -*-
"""
长期探针：定时对一个固定会话调 `AddListenChat`，把成败和当时的环境记成 JSONL，
攒够失败样本再回头找规律。

为什么需要它：真实失败率约 5%，而失败只在真人发消息时才暴露，一天撞不上几次。
手工诊断连跑 18 轮一次没复现，靠人守着等不来。探针每 10 分钟自动打一次，
一天 144 个样本，几天就能看出分布。

★ 必须跑在 bot 进程内、由 schedule 主循环串行调用。
  独立进程/线程做微信 UI 操作会和主循环抢窗口（CLAUDE.md 3.11 血泪：
  18:44 抢焦点导致粘贴打在主窗口上，连败 3 次）。

靶子默认用「文件传输助手」：系统会话，开关它的独立窗口不打扰真人、不产生已读回执。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from .config import DATA_DIR, load

try:
    from logger import log as _log
except Exception:
    def _log(level="INFO", message=""):
        print(f"[{level}] {message}")


def log(level: str, message: str) -> None:
    try:
        _log(level=level, message=f"[listen_health] {message}")
    except Exception:
        pass


_consecutive_fail = 0


def _visible_chat_windows():
    """当前微信可见聊天窗口数；拿不到（非 Windows / 缺依赖）返回 None。"""
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return None
    count = [0]

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in ('weixin.exe', 'wechat.exe'):
                return
        except Exception:
            return
        try:
            if win32gui.GetClassName(hwnd) == 'Qt51514QWindowIcon' and win32gui.IsWindowVisible(hwnd):
                count[0] += 1
        except Exception:
            return

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None
    return count[0]


def _record(row: dict) -> None:
    """一行 JSON 落盘。写失败只记日志，绝不往上抛。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"probe-{datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log("WARNING", f"探针写盘失败：{e}")


def _uptime_hours(bot):
    try:
        return round((datetime.now() - bot.start_time).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def tick(bot) -> None:
    """跑一轮探针。任何异常都不能冒泡到 schedule 主循环。"""
    global _consecutive_fail
    try:
        cfg = load()
        pcfg = cfg.get('probe', {})
        if not pcfg.get('enabled', True):
            return
        target = pcfg.get('target') or '文件传输助手'
        wx = getattr(bot, 'wx', None)
        if wx is None:
            return

        # 目标万一已经是被正式监听的会话，跳过本轮，别去动人家的窗口
        try:
            existing = [getattr(s, 'who', s) for s in (wx.GetAllSubWindow() or [])]
            if target in [str(x) for x in existing]:
                log("INFO", f"探针目标 {target} 已在监听中，跳过本轮")
                return
        except Exception:
            existing = None

        win_before = _visible_chat_windows()
        err = None
        t0 = time.time()
        try:
            wx.AddListenChat(nickname=target, callback=lambda msg: None)
        except Exception as e:
            err = repr(e)
        cost = round(time.time() - t0, 2)

        sub = None
        try:
            sub = wx.GetSubWindow(nickname=target)
        except Exception as e:
            if err is None:
                err = f"GetSubWindow: {e!r}"
        ok = err is None and sub is not None
        win_after = _visible_chat_windows()

        # 清理，别让探针窗口累积
        try:
            wx.RemoveListenChat(target)
        except Exception as e:
            log("WARNING", f"探针清理监听失败：{e}")

        _record({
            "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "target": target,
            "ok": ok,
            "err": err,
            "cost_sec": cost,
            "win_before": win_before,
            "win_after": win_after,
            "subwin_count": (len(existing) if isinstance(existing, list) else None),
            "bot_uptime_h": _uptime_hours(bot),
            "dyn_listen_count": len(getattr(bot, 'all_Mode_listen_list', []) or []),
            "msg_received": getattr(bot, 'msg_received_count', None),
        })

        if ok:
            if _consecutive_fail:
                log("INFO", f"探针恢复正常（此前连续失败 {_consecutive_fail} 次）")
            _consecutive_fail = 0
        else:
            _consecutive_fail += 1
            log("WARNING", f"探针第 {_consecutive_fail} 次连续失败：{err}")
            threshold = int(pcfg.get('alert_after_consecutive', 3))
            if _consecutive_fail == threshold:
                try:
                    from .alert import alert_listen_failure
                    alert_listen_failure(
                        bot, f"[探针] {target}",
                        retry_count=_consecutive_fail, last_error=err,
                        window_count=win_before,
                    )
                except Exception as e:
                    log("WARNING", f"探针告警失败：{e}")
    except Exception as e:
        log("ERROR", f"探针本轮出错（已吞掉）：{e}")


def register(bot, schedule) -> None:
    """挂进 bot 的 schedule。由 wxbot_core 在定时任务注册处调用。

    间隔取自配置，改间隔要重启机器人线程才生效（schedule 注册时就固定了）。
    """
    cfg = load()
    pcfg = cfg.get('probe', {})
    if not pcfg.get('enabled', True):
        log("INFO", "探针已关闭，不挂载")
        return
    interval = max(1, int(pcfg.get('interval_min', 10)))
    schedule.clear("listen_probe")
    schedule.every(interval).minutes.do(tick, bot).tag("listen_probe")
    # 主循环只在有定时任务开关打开时才 run_pending，这个标志让它把我们也算上
    bot._listen_probe_enabled = True
    log("INFO", f"监听探针已挂载：每 {interval} 分钟对「{pcfg.get('target') or '文件传输助手'}」打一次")
