# -*- coding: utf-8 -*-
"""NCC 知识库只读客户端：POST {kb_url}/retrieve。失败返回一句"检索不可用"，让模型按不知道处理。

★ 绕开环境里的代理：urllib 默认吃 HTTP_PROXY/HTTPS_PROXY，2026-09-05 回放时 mac 上的 shell 带着
`HTTP_PROXY=http://127.0.0.1:7890`，知识库那个 Tailscale 地址被送进代理、回 HTTPError，两轮回放里
几十次 kb_search 一次都没到 mac-mini（curl 不认大写 HTTP_PROXY，所以手工验证全通过、掩盖了它）。
与 CLAUDE.md 3.12「AI 调用绕开系统代理」是同一类病。
"""
from __future__ import annotations
import json
import urllib.request

FACTS_MAX_CHARS = 6000
CONTEXT_MAX_CHARS = 12000
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 空代理表 = 直连


def search(kb_url: str, query: str, timeout: float) -> str:
    try:
        req = urllib.request.Request(kb_url.rstrip("/") + "/retrieve",
                                     data=json.dumps({"query": query}, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with _OPENER.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return f"检索不可用（{type(e).__name__}: {str(e)[:80]}）。不要编造，按不知道处理，建议对方问群里的主理人。"
    parts = []
    if d.get("facts"):
        facts = d["facts"].strip()
        if len(facts) > FACTS_MAX_CHARS:
            facts = facts[:FACTS_MAX_CHARS] + "\n…（已截断）"
        parts.append("【固定事实清单（优先级最高）】\n" + facts)
    if d.get("context"):
        context = d["context"].strip()
        if len(context) > CONTEXT_MAX_CHARS:
            context = context[:CONTEXT_MAX_CHARS] + "\n…（已截断）"
        parts.append("【检索片段】\n" + context)
    if not parts:
        return "知识库里没有相关内容。不要编造。"
    return "\n\n".join(parts)
