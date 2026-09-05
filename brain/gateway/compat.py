# -*- coding: utf-8 -*-
"""OpenAI 兼容过渡路径的解析。机器人的 OpenAIAPI 发来 system + 历史 + 最后一条 user；
群消息正文是 `昵称: 内容`（wxbot_core.py:3569 `content_with_sender`），历史条目可能带 `[时间] ` 前缀。
会话身份藏在模型名里：`feirou:group:<群名>` / `feirou:chat:<昵称>`。
"""
from __future__ import annotations
import re
import time
from typing import List, Tuple

_PREFIX = re.compile(r"^(?:\[[^\]]{1,40}\]\s*)?([^:：\n]{1,30})[:：]\s*")
API_ERROR_TEXT = "API返回错误，请稍后再试"   # 与 plugins/model_fallback/chain.py 同一串，让机器人走备用链
NO_REPLY_TOKEN = "[NO_REPLY]"


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def parse_model(model: str) -> Tuple[str, bool]:
    m = re.match(r"^feirou:(group|chat):(.+)$", model or "")
    if m:
        return m.group(2).strip(), m.group(1) == "group"
    return (model or "").strip(), False


def _split(content: str) -> Tuple[str, str]:
    m = _PREFIX.match(content or "")
    if m:
        return m.group(1).strip(), content[m.end():].strip()
    return "", (content or "").strip()


def parse_last_user(messages: List[dict]) -> Tuple[str, str]:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return _split(_text(msg.get("content")))
    return "", ""


def history_to_prime(messages: List[dict]) -> List[dict]:
    users = [i for i, m in enumerate(messages or []) if m.get("role") == "user"]
    if not users:
        return []
    out = []
    for m in (messages or [])[:users[-1]]:
        role = m.get("role")
        if role == "user":
            sender, text = _split(_text(m.get("content")))
            out.append({"time": "", "type": "text", "attr": "friend", "sender": sender or "对方", "content": text})
        elif role == "assistant":
            out.append({"time": "", "type": "text", "attr": "self", "sender": "肥肉", "content": _text(m.get("content"))})
    return out


def to_completion(result: dict, model: str) -> dict:
    if result.get("bubbles"):
        content = "||SPLIT||".join(result["bubbles"])
    elif result.get("no_reply"):
        content = NO_REPLY_TOKEN
    else:
        content = API_ERROR_TEXT
    return {"id": "feirou-" + str(int(time.time() * 1000)), "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
