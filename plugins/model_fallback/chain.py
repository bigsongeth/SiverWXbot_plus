# -*- coding: utf-8 -*-
"""FallbackAPI：接口失败时自动换下一个模型，对上层完全透明。

刻意不 import wxbot_core —— 那会连带拉起 wxautox，mac 上就没法裸跑单测了。
需要用到的两样东西（日志、按索引造接口实例）都靠外部注入。
"""
from __future__ import annotations

import inspect

# 底层四个 API 类吞掉异常后统一返回的固定串（wxbot_core.py 里共 7 个 return 点）。
# 上层就是靠它 == 判断"这次调用失败了"，我们也只能跟着认这个串。
API_ERROR_TEXT = "API返回错误，请稍后再试"

# 底层自己吞掉异常、只留一个占位串的情况：真正的原因（状态码等）在它自己那条
# 「Chat Completions API 调用失败」日志里，排查时往上翻一条。
PLACEHOLDER_HINT = "返回错误占位串，具体原因见紧邻的上一条接口日志"


def _log(level, message):
    """尽量走项目统一的 log()，拿不到（单测环境）就退到 print。"""
    try:
        from wxbot_core import log
        log(level=level, message=message)
    except Exception:
        print(f"[{level}] {message}")


def is_failure(reply):
    """判定一次 chat() 的返回值算不算失败。

    底层把异常吞成固定串了，所以"没抛异常"不等于"成功"。
    """
    if reply is None:
        return True
    if not isinstance(reply, str):
        return False
    stripped = reply.strip()
    return stripped == "" or stripped == API_ERROR_TEXT


def api_identity(api):
    """接口身份，用于避免在一次调用里重复试同一个接口。

    四个 API 类都有 DS_NOW_MOD / base_url / api_key，取不到就按空串算。
    key 只取前 8 位，日志和内存里都不留完整密钥。
    """
    key = str(getattr(api, "api_key", "") or "")
    return (
        str(getattr(api, "base_url", "") or ""),
        str(getattr(api, "DS_NOW_MOD", "") or ""),
        key[:8],
    )


def _describe(api):
    return f"{type(api).__name__}/{getattr(api, 'DS_NOW_MOD', '?')}"


def _filter_kwargs(func, kwargs):
    """剔除目标 chat() 不接受的关键字参数。

    四个类的 chat() 签名不齐：OpenAIAPI / DusAPI 收 image_path、image_url，
    DifyAPI / CozeAPI 不收。主接口是 OpenAI、备用是 Dify 时，原样透传会 TypeError，
    那就成了"备用接口也失败"，白白浪费一个备用。这里按签名裁一刀。
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return dict(kwargs), []
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs), []
    accepted = {k: v for k, v in kwargs.items() if k in params}
    dropped = [k for k in kwargs if k not in params]
    return accepted, dropped


class FallbackAPI:
    """按顺序试多个接口，第一个成功就返回。

    对外只需长得像一个 API 实例：有 chat()，其余属性代理到主接口
    （上层有 `self.api.DS_NOW_MOD` 这类读法）。
    """

    def __init__(self, primary, backup_factories, session_name=""):
        """
        primary          : 该会话原本该用的接口实例
        backup_factories : [(标签, 无参工厂函数)]，惰性调用——备用接口不到真失败不实例化
        session_name     : 群名/联系人名，只用于日志
        """
        self._primary = primary
        self._backup_factories = list(backup_factories or [])
        self._session_name = session_name or ""
        self._backup_cache = {}

    def __getattr__(self, name):
        # __init__ 里赋的下划线属性走不到这里；其余一律代理给主接口
        return getattr(self._primary, name)

    def _iter_backups(self):
        """惰性产出备用接口实例，造不出来的（配置写错等）跳过。"""
        for pos, (label, factory) in enumerate(self._backup_factories):
            if pos not in self._backup_cache:
                try:
                    self._backup_cache[pos] = factory()
                except Exception as e:
                    _log("ERROR", f"[fallback] 备用接口 {label} 初始化失败，跳过：{e}")
                    self._backup_cache[pos] = None
            api = self._backup_cache[pos]
            if api is not None:
                yield label, api

    def chat(self, message, *args, **kwargs):
        who = f"[{self._session_name}] " if self._session_name else ""
        tried = {api_identity(self._primary)}

        reply, err = self._try_one(self._primary, message, args, kwargs)
        if err is None and not is_failure(reply):
            return reply
        _log("WARNING", f"[fallback] {who}主接口 {_describe(self._primary)} 失败"
                        f"（{err or PLACEHOLDER_HINT}），开始尝试备用接口")

        for label, api in self._iter_backups():
            ident = api_identity(api)
            if ident in tried:
                _log("INFO", f"[fallback] {who}备用接口 {label} 与已试过的是同一个，跳过")
                continue
            tried.add(ident)

            reply, err = self._try_one(api, message, args, kwargs)
            if err is None and not is_failure(reply):
                _log("SUCCESS", f"[fallback] {who}已切换到备用接口 {label}"
                                f"（{_describe(api)}）并成功回复")
                return reply
            _log("WARNING", f"[fallback] {who}备用接口 {label}（{_describe(api)}）"
                            f"同样失败（{err or PLACEHOLDER_HINT}）")

        _log("ERROR", f"[fallback] {who}主接口与全部 {len(self._backup_factories)} 个"
                      f"备用接口均失败，交回上层走固定回复")
        # 交回上层认识的固定串，后续照旧走 api_error_reply，行为与没装本插件时一致
        return API_ERROR_TEXT

    def _try_one(self, api, message, args, kwargs):
        """调一次，返回 (reply, 错误描述)。成功时错误描述为 None。"""
        safe_kwargs, dropped = _filter_kwargs(api.chat, kwargs)
        if dropped:
            _log("INFO", f"[fallback] {_describe(api)} 不支持参数 {dropped}，已忽略后调用"
                         f"（图片类消息会退化成纯文字）")
        try:
            return api.chat(message, *args, **safe_kwargs), None
        except Exception as e:
            # 上游报错常带整页 HTML，截断免得刷屏；状态码通常在开头（"Error code: 529 - ..."）
            detail = " ".join(str(e).split())
            if len(detail) > 200:
                detail = detail[:200] + "…"
            return None, f"{type(e).__name__}: {detail}"


def build_backup_factories(config, api_factory, log=None):
    """把 config 里的 fallback_chain 翻成 [(标签, 工厂)]。

    config     : 主配置 dict（含 fallback_switch / fallback_chain / api_configs）
    api_factory: 形如 bot._init_api_by_index 的函数，吃索引吐接口实例
    """
    log = log or _log   # 晚绑定，单测才能替换掉 _log 静音
    if not config.get("fallback_switch"):
        return []
    chain = config.get("fallback_chain") or []
    if not isinstance(chain, (list, tuple)):
        log("WARNING", f"[fallback] fallback_chain 不是列表（{type(chain).__name__}），已忽略")
        return []

    api_configs = config.get("api_configs") or []
    factories = []
    seen = set()
    for raw in chain:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            log("WARNING", f"[fallback] fallback_chain 里的 {raw!r} 不是索引，已跳过")
            continue
        if idx < 0 or idx >= len(api_configs):
            log("WARNING", f"[fallback] fallback_chain 里的索引 {idx} 越界"
                           f"（当前共 {len(api_configs)} 个接口），已跳过")
            continue
        if idx in seen:
            continue
        seen.add(idx)
        cfg = api_configs[idx]
        label = f"接口{idx + 1}({cfg.get('model') or cfg.get('sdk') or '未命名'})"
        factories.append((label, lambda i=idx: api_factory(i)))
    return factories
