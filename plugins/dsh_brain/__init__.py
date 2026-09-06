# -*- coding: utf-8 -*-
"""dsh_brain 插件：把指定群/私聊的 AI 回复交给肥肉大脑（brain/，常驻 dsh 智能体 + 网关）。

与上游零冲突：wxbot_core.py 只在 _resolve_group_api / _resolve_chat_api 各加一段最小 hook，
先问本插件"这个会话交给大脑了吗"，交了就返回 BrainAPI（长得和四个接口类一样），
没交走原逻辑（group_api_map → 默认接口）。人设/知识库/技能都在大脑那边，
机器人不再挑接口和人设。设计见 docs/superpowers/specs/2026-09-05-dsh-brain-design.md §5，
跑法见 brain/README.md。配置 data/config.json（不进库，默认全关）。
"""
from __future__ import annotations

from . import store
from .client import BrainAPI

WILDCARD = "*"
_api_cache = {}


def _norm(s) -> str:
    return str(s or "").strip()


def brain_enabled(who, is_group: bool) -> bool:
    cfg = store.load()
    if not cfg.get("enabled"):
        return False
    who_n = _norm(who)
    ex_key = "excluded_groups" if is_group else "excluded_chats"
    if who_n in {_norm(x) for x in cfg.get(ex_key, []) or []}:
        return False
    key = "enabled_groups" if is_group else "enabled_chats"
    items = {_norm(x) for x in cfg.get(key, []) or []}
    return WILDCARD in items or who_n in items


def brain_api_for(who, is_group: bool):
    """该会话交给大脑就返回 BrainAPI 实例（按会话缓存），否则 None（走上游原逻辑）。"""
    if not brain_enabled(who, is_group):
        return None
    cfg = store.load()
    key = (bool(is_group), _norm(who), cfg.get("gateway_url"), cfg.get("timeout_sec"))
    api = _api_cache.get(key)
    if api is None:
        api = BrainAPI(_norm(who), bool(is_group), str(cfg.get("gateway_url") or ""), float(cfg.get("timeout_sec") or 300))
        _api_cache[key] = api
    return api
