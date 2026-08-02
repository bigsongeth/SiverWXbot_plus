# -*- coding: utf-8 -*-
"""model_fallback 插件：某个模型挂了（500/403/529/超时）时自动换下一个接口重答同一个问题。

面板原来只能"选一个接口用"，挂了就直接回"在忙，我稍后回复您"。本插件在
`_get_group_api` / `_get_chat_api` 的返回值外面套一层 FallbackAPI：主接口失败就沿
`fallback_chain` 往下试，成功即返回。历史/人设/分段/图片/接话闸门全走上游原链路，
用户体感只是这次慢了两秒，问题不用重发。

配置跟 api_configs 同住 config/config.json，由现有的"API 接口配置"面板一起保存：
  fallback_switch : 总开关
  fallback_chain  : api_configs 的索引数组，有序，从前往后试
详见 SPEC.md。改完要重启机器人线程才生效（和改 api_configs 一样）。
"""
from __future__ import annotations

from .chain import (
    API_ERROR_TEXT,
    FallbackAPI,
    api_identity,
    build_backup_factories,
    is_failure,
)

__all__ = [
    "API_ERROR_TEXT",
    "FallbackAPI",
    "api_identity",
    "build_backup_factories",
    "is_failure",
    "wrap",
]

# (bot id, 主接口身份) -> FallbackAPI，避免每条消息都重建备用接口实例
_wrap_cache = {}


def wrap(bot, api, session_name=""):
    """给一个接口实例套上故障转移；开关没开或链为空时原样返回。

    调用点只有 wxbot_core.py 的 _get_group_api / _get_chat_api 两处。
    任何异常都吞掉并返回原实例——插件坏了不能把正常问答一起带走。
    """
    if api is None:
        return api
    try:
        raw_config = getattr(bot.config, "config", None) or {}
        if not raw_config.get("fallback_switch"):
            return api

        cache_key = (id(bot), api_identity(api))
        cached = _wrap_cache.get(cache_key)
        if cached is not None:
            # session_name 只用于日志，换个会话直接改字段，不必重建备用实例
            cached._session_name = session_name or ""
            return cached

        factories = build_backup_factories(raw_config, bot._init_api_by_index)
        if not factories:
            return api

        wrapped = FallbackAPI(api, factories, session_name)
        _wrap_cache[cache_key] = wrapped
        return wrapped
    except Exception as e:
        try:
            from wxbot_core import log
            log(level="ERROR", message=f"model_fallback wrap 失败，按无故障转移处理：{e}")
        except Exception:
            pass
        return api


def reset_cache():
    """清空包装缓存（改完接口配置重启机器人线程时用，单测也用）。"""
    _wrap_cache.clear()
