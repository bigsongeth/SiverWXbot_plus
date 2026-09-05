# -*- coding: utf-8 -*-
"""把一条微信消息变成喂给大脑的用户消息：前缀行 + （首次）历史预热 + 正文。"""
from __future__ import annotations
import json
import os
import re
from typing import List, Optional

from plugins.context_guard import guard as _guard
from plugins.context_guard.guard import filter_history  # 时间戳条目/兜底文案/[NO_REPLY]/"没法联网"整轮连坐
from plugins.wechat_checkin.handler import TRIGGERS, normalize_text  # 签到触发词的唯一真相源

# 签到往来不进大脑（设计 §2.4）：触发词整条命中（去空白标点后）、兑换码、兑换站点
_CHECKIN_TAIL = re.compile(r"(BTC-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}|key\.bigsong\.site|兑换码)")


def is_checkin_text(text: str) -> bool:
    t = normalize_text(text or "")
    return t in TRIGGERS or bool(_CHECKIN_TAIL.search(text or ""))


def load_skill_index(workspace_dir: str) -> dict:
    path = os.path.join(workspace_dir, "skills", "index.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def match_skills(index: dict, conversation: str, is_group: bool) -> List[str]:
    out = []
    for name, meta in index.items():
        if meta.get("scope") == "all":
            continue
        if is_group and conversation in (meta.get("groups") or []):
            out.append(name)
        if not is_group and conversation in (meta.get("chats") or []):
            out.append(name)
    out += [n for n, m in index.items() if m.get("scope") == "all"]
    return out


def _is_dropped_self_fallback(item: dict) -> bool:
    """兜底文案/[NO_REPLY]/"没法联网"这类脏话，不该管 context_guard 插件的运行时开关。

    `filter_history` 一旦读到插件配置 `enabled=false` 或 `filter_history=false` 就原样
    透传——那个开关管的是"微信机器人实际发出去的历史"要不要洗，不该连带决定"大脑预热时
    看到的历史"要不要洗。这里直接从 guard 的默认词表判断，是一道不看插件开关的独立防线。
    """
    if item.get("attr") != "self":
        return False
    content = str(item.get("content", "")).strip()
    if not content:
        return False
    cfg = _guard._DEFAULT_CONFIG
    if content in cfg["drop_assistant_contents"]:
        return True
    return any(s in content for s in cfg["drop_assistant_substrings"])


def filter_prime(items: List[dict], count: int) -> List[dict]:
    kept = [x for x in filter_history(list(items or []))
            if x.get("attr") in ("friend", "self") and x.get("type", "text") == "text"
            and not is_checkin_text(str(x.get("content", "")))
            and not _is_dropped_self_fallback(x)]
    return kept[-count:]


def build_user_message(conversation: str, is_group: bool, sender: str, text: str, now_str: str,
                       skills: List[str], prime: Optional[List[dict]] = None) -> str:
    kind = "群聊" if is_group else "私聊"
    head = f"[{kind}:{conversation} | 发言人:{sender} | {now_str} | 相关技能:{','.join(skills) if skills else '无'}]"
    parts = [head]
    if prime:
        lines = []
        for x in prime:
            who = f"你({x.get('sender')})" if x.get("attr") == "self" else x.get("sender")
            lines.append(f"[{x.get('time')}] {who}: {x.get('content')}")
        parts.append("以下是此前的聊天记录，只供了解背景，不要逐条回复：\n" + "\n".join(lines) + "\n---")
    parts.append(f"{sender}: {text}")
    return "\n".join(parts)
