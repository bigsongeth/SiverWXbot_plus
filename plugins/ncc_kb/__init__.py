# -*- coding: utf-8 -*-
"""ncc_kb 插件：给指定群聊/私聊"接入知识库"开关。

设计（与上游零冲突）：
- wxbot_core.py 的四个 getter（_get_group_api/_get_chat_api/_get_group_prompt/
  _get_chat_prompt）各加一段最小 hook：先问本插件"这个会话开知识库了吗？"，
  开了就返回知识库的 API 实例 + NCC 人设 prompt；没开就走上游原逻辑。
- "开知识库" = 走 mac-mini 的 rag_proxy 端点（OpenAI 兼容）+ NCC肥肉 人设；
  "关知识库" = 自动回落到该会话原本配置的接口/人设（比如群 group_api_map 指的那个）。
- 复用上游现成的 AI 调用链路（历史记忆/分段/图片识别等全不动），只换"用哪个接口 + 哪个人设"。

配置在 data/config.json，面板 /ncc_kb 页可视化增删；管理群指令也能改（见 store）。
"""
from __future__ import annotations

from . import store

# 端点 -> API 实例缓存。端点变了 key 就变，自动重建，旧实例留存无害。
_api_cache = {}


def _norm(s) -> str:
    return str(s or "").strip()


def kb_enabled(who, is_group) -> bool:
    cfg = store.load()
    key = "enabled_groups" if is_group else "enabled_chats"
    return _norm(who) in {_norm(x) for x in cfg.get(key, [])}


def _build_api(endpoint):
    """按端点配置构建一个 AI 接口实例（复用 wxbot_core 的接口类，懒导入避免循环依赖）。"""
    from wxbot_core import DusAPI, OpenAIAPI

    class _Proxy:
        pass

    p = _Proxy()
    p.api_sdk = endpoint.get("sdk", "DusAPI")
    p.api_key = endpoint.get("key", "")
    p.base_url = endpoint.get("url", "")
    p.model1 = endpoint.get("model", "")
    p.prompt = ""  # prompt 总是 chat() 显式传入
    if p.api_sdk == "OpenAI SDK":
        return OpenAIAPI(p)
    return DusAPI(p)


def kb_api_for(bot, who, is_group):
    """会话开了知识库则返回知识库 API 实例，否则返回 None（上游走原逻辑）。"""
    if not kb_enabled(who, is_group):
        return None
    ep = store.load().get("endpoint", {}) or {}
    key = (ep.get("sdk"), ep.get("url"), ep.get("key"), ep.get("model"))
    if key not in _api_cache:
        _api_cache[key] = _build_api(ep)
    return _api_cache[key]


def kb_prompt_for(bot, who, is_group):
    """会话开了知识库则返回 NCC 人设 prompt 内容，否则返回 None。"""
    if not kb_enabled(who, is_group):
        return None
    name = _norm(store.load().get("prompt_name"))
    if not name:
        return None
    try:
        content = bot.config.get_prompt_content(name)
    except Exception:
        return None
    return content or None


# ---- 面板 / 指令用的配置读写 ----

def get_config() -> dict:
    return store.load()


def save_config(cfg: dict) -> dict:
    store.save(cfg)
    return cfg


def set_enabled(names, is_group) -> dict:
    """整体覆盖启用列表（面板保存时用）。"""
    cfg = store.load()
    cleaned, seen = [], set()
    for n in names or []:
        v = _norm(n)
        if v and v not in seen:
            cleaned.append(v)
            seen.add(v)
    cfg["enabled_groups" if is_group else "enabled_chats"] = cleaned
    store.save(cfg)
    return cfg


def toggle(name, is_group, on) -> dict:
    """开/关单个会话（管理群指令用）。"""
    cfg = store.load()
    key = "enabled_groups" if is_group else "enabled_chats"
    lst = [_norm(x) for x in cfg.get(key, [])]
    name = _norm(name)
    if on and name not in lst:
        lst.append(name)
    if not on:
        lst = [x for x in lst if x != name]
    cfg[key] = lst
    store.save(cfg)
    return cfg
