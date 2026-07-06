# -*- coding: utf-8 -*-
"""被动发现新群 —— 引擎的④。

肥肉被拉进新群、群里一有人说话，就自动：
  登记为 pending → 打🐶备注 → 写进 Notion 待归类 → 管理群提醒。
（用户决策：自动打备注+入库+提醒）

只对【群消息】且【未登记】的会话触发，且用进程内去重避免同一群反复处理。
微信操作（打备注）走 bot.wx，同进程、持 MAIN_WINDOW_LOCK。
"""
from __future__ import annotations

import threading

from . import registry, remark
from .common import log, REPLY_PREFIX

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

    # 写入 Notion 待归类（失败不影响本地登记）
    notion_note = ""
    try:
        from . import notion_sync
        notion_sync.push_discovery(who)
        notion_note = "，已加入 Notion 待归类"
    except Exception as e:
        notion_note = f"（写 Notion 失败：{e}）"
        log("WARNING", f"新群写 Notion 失败 {who}: {e}")

    _notify_admin(bot, cfg,
                  f"发现新群「{who}」{applied_note}{notion_note}。\n"
                  f"请去 Notion『群聊列表』给它选分组、勾选允许转发/发言。")


def _registry_name(data: dict, who: str) -> str:
    name, _ = registry.find_by_chat_who(data, who)
    return name or who


def _notify_admin(bot, cfg, text: str) -> None:
    admin = cfg.get("admin_group")
    if not admin:
        return
    try:
        bot.wx.SendMsg(msg=f"{REPLY_PREFIX} {text}", who=admin)
    except Exception as e:
        log("ERROR", f"提醒管理群失败: {e}")
