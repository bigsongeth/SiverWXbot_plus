# -*- coding: utf-8 -*-
"""把两种真实数据源变成统一的回放序列（Turn）。

Turn = {conversation, is_group, sender, text, old_reply: str, prime: list}

- qa jsonl（mac-mini ~/ncc-kb/logs/qa-*.jsonl）：一行一问一答，query 有时带 "昵称: " 前缀。当私聊回放，没有 prime。
- 机器人 memory json（memory/<wxid>/<会话>/<会话>_memory.json）：流水，friend 之后连着的 self 是老回复。
  ★ 机器人的引用回复 type 是 "quote" 不是 "text"（真机样本），所以 friend/self 都收 text/quote 两种；
    attr=system（type=time）、图片/链接/表情等一律排除。
两边都过滤签到往来，判据复用网关的 is_checkin_text（和 prime 过滤同一把尺）。
"""
from __future__ import annotations
import json
import re
from typing import List

from brain.gateway.context import is_checkin_text

_PREFIX = re.compile(r"^([^:：\n]{1,20})[:：]\s*")
_KEEP_TYPES = ("text", "quote")


def _split_sender(query: str):
    m = _PREFIX.match(query or "")
    if m:
        return m.group(1).strip(), query[m.end():].strip()
    return "对方", (query or "").strip()


_SPLIT = re.compile(r"\s*\|\|SPLIT\|\|\s*")


def _clean_reply(s) -> str:
    """分段标记及其两侧空白折成一个换行（真机日志里是 "\n||SPLIT||\n"）。"""
    return _SPLIT.sub("\n", str(s or "")).strip()


def from_qa_jsonl(path: str) -> List[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            d = json.loads(ln)
            q = d.get("query") or ""
            if is_checkin_text(q) or is_checkin_text(d.get("answer") or ""):
                continue
            sender, text = _split_sender(q)
            if not text:
                continue
            out.append({"conversation": "sim-qa-%s" % sender, "is_group": False, "sender": sender, "text": text,
                        "old_reply": _clean_reply(d.get("answer")), "prime": []})
    return out


def _as_prime(x: dict) -> dict:
    """prime 条目：quote 归一成 text（只改副本；网关 filter_prime 现在两种都收，这里保留只为老对照表可比）。"""
    y = dict(x)
    y["type"] = "text"
    y["content"] = str(x.get("content", ""))
    return y


def from_memory_json(path: str, conversation: str, is_group: bool) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    items = [x for x in items
             if x.get("attr") in ("friend", "self") and x.get("type", "text") in _KEEP_TYPES
             and not is_checkin_text(str(x.get("content", "")))]
    out = []
    for i, x in enumerate(items):
        if x["attr"] != "friend":
            continue
        replies = []
        for y in items[i + 1:]:
            if y["attr"] != "self":
                break
            replies.append(_clean_reply(y.get("content")))
        out.append({"conversation": "sim-%s" % conversation, "is_group": is_group,
                    "sender": str(x.get("sender", "?")), "text": str(x.get("content", "")),
                    "old_reply": "\n".join(r for r in replies if r),
                    "prime": [_as_prime(p) for p in items[max(0, i - 20):i]]})
    return out
