# -*- coding: utf-8 -*-
"""被动发现新群 —— 引擎的④。

肥肉被拉进新群、群里一有人说话，就自动：
  登记为 pending → 打🐶备注 → 管理群提醒（去面板归类）。
（用户决策：自动打备注+入库+提醒。2026-08-05 起不再写 Notion，见 PANEL_SPEC.md）

只对【群消息】且【未登记】的会话触发，且用进程内去重避免同一群反复处理。
微信操作（打备注）走 bot.wx，同进程、持 MAIN_WINDOW_LOCK。
"""
from __future__ import annotations

import threading

from . import registry, remark
from .common import log, notify_admin

# 本次运行已处理过的新群，避免同一群每条消息都触发一轮
_SEEN = set()
_SEEN_LOCK = threading.Lock()

# 是否在发现时立刻打备注（可关：只登记+提醒，备注留给批量指令）
AUTO_APPLY_REMARK = True


def handle_discovery(bot, chat, msg, cfg) -> None:
    """群消息入口（在 friend 消息处理里、非管理群时调用）。不返回处理标志——
    发现逻辑是旁路，不拦截正常的转发/AI 流程。"""
    chat_type = str(getattr(chat, "chat_type", "") or "")
    if chat_type != "group":
        return
    who = str(getattr(chat, "who", "") or "")
    if not who or who == cfg.get("admin_group"):
        return

    data = registry.load()
    if registry.is_known(data, who):
        registry.touch_last_seen(_registry_name(data, who))
        return

    with _SEEN_LOCK:
        if who in _SEEN:
            return
        _SEEN.add(who)

    log("INFO", f"发现新群：{who}")
    registry.add_pending(who)

    applied_note = ""
    if AUTO_APPLY_REMARK:
        try:
            from .forward import MAIN_WINDOW_LOCK
            with MAIN_WINDOW_LOCK:
                ok, info = remark.apply_remark(bot.wx, who)
            applied_note = "，已打🐶备注" if ok else f"（打备注失败：{info}）"
        except Exception as e:
            applied_note = f"（打备注异常：{e}）"
            log("ERROR", f"发现新群打备注异常 {who}: {e}")

    # 去 Notion 化后不再往 Notion 写待归类行（PANEL_SPEC §1 #2）：
    # registry.add_pending 已经把它落到本地，面板「待归类」页直接就能看到并归类。
    from . import panel
    _notify_admin(bot, cfg,
                  f"发现新群「{who}」{applied_note}。\n"
                  f"去面板「待归类」给它选分组、勾允许转发/发言：\n{panel.panel_url()}")


def _registry_name(data: dict, who: str) -> str:
    name, _ = registry.find_by_chat_who(data, who)
    return name or who


def _notify_admin(bot, cfg, text: str) -> None:
    """实现已提到 common.notify_admin（拉群失败也要用），这里保留薄封装。"""
    notify_admin(bot, cfg, text)
