# -*- coding: utf-8 -*-
"""
动态监听失败告警。

在此之前，`AddListenChat` 连续失败只写一行 ERROR 日志就算完，
两次真实的消息丢失（08-03 King_🐕 签到、08-04 基司菲尔提问）都是靠人肉翻日志
事后才发现的。这里在最终失败时推一条出来。

设计取舍：
- **只在最终失败时告警**，中间那几次重试不报（间歇性故障，重试成了就没事）。
- 同一会话有冷却（默认 10 分钟），防止对方连发消息时刷屏。
- 任何一个通道挂了都吞掉异常 —— 告警链路绝不能反过来搞垮监听链路。
"""
from __future__ import annotations

import os
import time

from .config import load

try:
    from logger import log as _log
except Exception:  # 单测环境没有项目根的 logger
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


# {会话名: 上次告警时间戳}，进程内存活，重启即清空（可接受）
_last_alert = {}


def _should_alert(nickname: str, cooldown: int, now: float) -> bool:
    last = _last_alert.get(nickname)
    if last is not None and now - last < cooldown:
        return False
    _last_alert[nickname] = now
    return True


def build_message(nickname: str, retry_count: int, last_error, window_count=None) -> str:
    """组装告警正文。单独抽出来是为了能被单测直接验。"""
    lines = [
        f"会话：{nickname}",
        f"重试：{retry_count} 次后仍未拿到独立子窗口",
        f"最后异常：{last_error if last_error else '无（AddListenChat 未抛异常但子窗口校验不通过）'}",
    ]
    if window_count is not None:
        lines.append(f"当前可见聊天窗口：{window_count} 个")
    lines.append("影响：本条消息已回落主窗口处理，不会丢；该会话暂时没有独立监听窗口。")
    lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def alert_listen_failure(bot, nickname: str, retry_count: int = 3,
                         last_error=None, window_count=None) -> bool:
    """
    动态监听最终失败时调用。返回是否真的发出了告警（被冷却挡住返回 False）。
    绝不抛异常。
    """
    try:
        cfg = load()
        acfg = cfg.get('alert', {})
        if not acfg.get('enabled', True):
            return False
        if not _should_alert(nickname, int(acfg.get('cooldown_sec', 600)), time.time()):
            log("INFO", f"{nickname} 监听失败告警在冷却期内，跳过")
            return False

        title = f"动态监听添加失败：{nickname}"
        content = build_message(nickname, retry_count, last_error, window_count)
        sent = False

        if acfg.get('webhook', True):
            try:
                import webhook_send
                ok, msg = webhook_send.send_message(title, content)
                sent = sent or bool(ok)
                if not ok:
                    log("WARNING", f"webhook 告警失败：{msg}")
            except Exception as e:
                log("WARNING", f"webhook 告警异常：{e}")

        if acfg.get('admin_group', True):
            try:
                from plugins.ncc_community.common import notify_admin
                from plugins.ncc_community.store import load as load_ncc
                notify_admin(bot, load_ncc(), f"⚠️ {title}\n{content}")
                sent = True
            except Exception as e:
                log("WARNING", f"管理群告警异常：{e}")

        log("ERROR" if not sent else "INFO",
            f"{nickname} 监听失败告警{'已发出' if sent else '全部通道失败'}")
        return sent
    except Exception as e:
        log("ERROR", f"告警自身出错（已吞掉，不影响监听）：{e}")
        return False
