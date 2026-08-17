# -*- coding: utf-8 -*-
"""
自愈：探针确认进入坏状态后，自动重启机器人进程。

依据（2026-08-04，wxautox 作者 Siver 的回复 + 我们自己的日志）：
这个 1400 不是随机抖动，是「微信窗口丢失」——一旦发生就**持续**失败，重启才恢复。
日志佐证：08-03 20:34 失败、20:37 再失败；08-04 13:46 失败。两次都是重启程序后
立刻全好（00:20 和 15:09 各一次），而且**微信客户端全程没重启过**——
所以自愈只需要重启我们自己的进程，不需要人扫码登录，可以全自动。

策略：
  探针连续失败 N 次（默认 2 次 = 约 20 分钟，避开单次抖动）
    → 触发 SWXPanelRestart（只杀 web_server 的 python，不碰微信进程）
    → 冷却期内不再重启，防重启风暴
  冷却期内又连续失败 = 重启没解决 = 微信侧坏了
    → 升级告警叫人（这时候才需要人去重启微信客户端），且不再重启

状态落盘 data/heal_state.json —— 进程重启后内存全丢，必须靠文件才知道「刚刚已经
自愈过一次了」，否则会陷入无限重启。
"""
from __future__ import annotations

import json
import os
import time

from .config import DATA_DIR, load as load_config

try:
    from logger import log as _log
except Exception:
    def _log(level="INFO", message=""):
        print(f"[{level}] {message}")


def log(level: str, message: str) -> None:
    # 单测不许写进生产日志：本文件的用例会打出「触发 SWXPanelRestart 自愈重启」
    # 这类以假乱真的行，混进 panel_logs 后排查真故障时根本分不出真假（2026-08-15 踩到）。
    if os.environ.get("NCC_LOG_SILENT") == "1":
        print(f"[{level}] [listen_health] {message}")
        return
    try:
        _log(level=level, message=f"[listen_health] {message}")
    except Exception:
        pass


STATE_PATH = os.path.join(DATA_DIR, 'heal_state.json')


def load_state() -> dict:
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("WARNING", f"自愈状态写盘失败：{e}")


def _trigger_restart(task_name: str) -> None:
    """触发整进程重启。

    直接复用 ui_watchdog 的实现，别自己再写一遍 —— 那里踩过 schtasks 裸名字
    FileNotFoundError 的坑（2026-07-30 看门狗因此哑火一晚），已经改成
    %SystemRoot%\\System32 绝对路径。

    ★ 必须先写自启动标记再触发重启，两步缺一不可。2026-08-11 这里只复用了
    触发那一半、漏了写标记：面板重启起来了，机器人却没被拉起，从 16:05 一直
    下线到人发现为止（1.5 小时）。自愈把"监听坏了"升级成了"机器人没了"。
    """
    from plugins.ui_watchdog import _default_trigger, write_autostart_flag
    write_autostart_flag('listen_probe_restart')
    _default_trigger(task_name)


def maybe_heal(bot, consecutive_fail: int, pcfg: dict, last_error=None, snapshot=None) -> str:
    """
    探针失败后调用，决定是否自愈。返回 'none' | 'restart' | 'give_up'。
    绝不抛异常 —— 自愈失败最多是没自愈，不能把探针和主循环带下水。
    """
    try:
        if not pcfg.get('auto_restart', True):
            return 'none'
        threshold = int(pcfg.get('restart_after_consecutive', 2))
        if consecutive_fail < threshold:
            return 'none'

        cooldown = int(pcfg.get('restart_cooldown_min', 60)) * 60
        state = load_state()
        now = time.time()
        last = float(state.get('last_restart_ts') or 0)

        if now - last < cooldown:
            # 刚自愈过还是坏 —— 重启这条路走不通了，叫人
            if not state.get('escalated'):
                mins = int((now - last) / 60)
                _alert(bot,
                       "自动重启后仍然失败，需要人工介入",
                       f"{mins} 分钟前已自动重启过一次机器人进程，探针仍连续失败 "
                       f"{consecutive_fail} 次。\n"
                       f"最后异常：{last_error}\n"
                       f"环境：{snapshot}\n"
                       f"下一步：请远程上机重启微信客户端（程序重启已证明无效）。")
                state['escalated'] = True
                save_state(state)
            log("ERROR", f"自愈冷却期内仍失败（{consecutive_fail} 次），已升级告警，不再重启")
            return 'give_up'

        # 触发自愈重启
        task = pcfg.get('restart_task_name', 'SWXPanelRestart')
        _alert(bot,
               "监听进入坏状态，即将自动重启机器人",
               f"探针连续失败 {consecutive_fail} 次，判定微信窗口丢失。\n"
               f"最后异常：{last_error}\n"
               f"环境：{snapshot}\n"
               f"处理：触发 {task} 重启机器人进程（不动微信客户端，无需重新登录）。")
        state['last_restart_ts'] = now
        state['escalated'] = False
        state['restart_count'] = int(state.get('restart_count') or 0) + 1
        save_state(state)   # 必须先落盘再重启，否则进程没了状态就丢了

        log("ERROR", f"探针连续失败 {consecutive_fail} 次，触发 {task} 自愈重启")
        _trigger_restart(task)
        return 'restart'
    except Exception as e:
        log("ERROR", f"自愈流程出错（已吞掉）：{e}")
        return 'none'


def _alert(bot, title: str, content: str) -> None:
    """自愈相关的通知，走和失败告警同一套通道，但不吃它的冷却（这类事件本来就少）。

    ★★ "同一套通道"必须【真的】读同一套开关（2026-08-15 修）：
    原来这里把 webhook + 管理群写死成两边都发，压根不看 `alert.webhook` /
    `alert.admin_group`。于是人把 `admin_group` 关成 false（约定是运维状态消息只发飞书）
    之后，探针告警确实不发微信了，而【自愈这一路】——"监听进入坏状态，即将自动重启
    机器人"——照旧往管理群里灌。开关看着生效了一半，人只会以为"我明明关了"。
    注释自称同一套、实现却各走各的，是这类 bug 最好的藏身处。"""
    acfg = (load_config() or {}).get('alert', {})
    if acfg.get('webhook', True):
        try:
            import webhook_send
            webhook_send.send_message(title, content)
        except Exception as e:
            log("WARNING", f"自愈 webhook 通知失败：{e}")
    if acfg.get('admin_group', True):
        try:
            from plugins.ncc_community.common import notify_admin
            from plugins.ncc_community.store import load as load_ncc
            notify_admin(bot, load_ncc(), f"⚠️ {title}\n{content}")
        except Exception as e:
            log("WARNING", f"自愈管理群通知失败：{e}")
