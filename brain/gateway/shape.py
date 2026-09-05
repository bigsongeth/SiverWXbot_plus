# -*- coding: utf-8 -*-
"""回复形状的纯函数。不 import 任何网关状态，mac 上裸跑单测。

规则来自设计文档 §4.4：长度预算、剥 Markdown、剥收尾套话、反口头禅、截断。
"""
from __future__ import annotations
import re
from typing import Iterable, List, Optional

_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")
_SENT_END = "。！!？?~～"
_CLOSING = re.compile(r"(需要我|要不要|还要|想知道|继续吗|要我|想听)")


def text_len(s: Optional[str]) -> int:
    """算字数预算用：空白不算，链接也不算（美食地图一条短链 38 字符，三家店就把群聊预算吃光了；
    链接是给人点的，不是"话"）。"""
    return len(_WS.sub("", _URL.sub("", s or "")))


def budget(incoming: str, is_group: bool, cfg: dict) -> int:
    b = cfg["budget"]
    cap = b["max_group"] if is_group else b["max_private"]
    raw = b["base"] + b["factor"] * text_len(incoming)
    return int(max(b["min"], min(cap, raw)))


def max_bubbles(is_group: bool, cfg: dict) -> int:
    return int(cfg["max_bubbles_group"] if is_group else cfg["max_bubbles_private"])


# 剥 Markdown 复用机器人插件那份（只剥 **加粗** / # 标题 / --- 分隔线，保留列表符号与代码块），别再写一份。
from plugins.reply_shape import strip_markdown  # noqa: E402,F401


def _split_sentences(s: str) -> List[str]:
    parts, buf = [], ""
    for ch in s:
        buf += ch
        if ch in _SENT_END:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    return [p for p in parts if p.strip()]


def strip_closing(bubbles: List[str]) -> List[str]:
    """最后一条以问号结尾且含套话 → 整条剥掉；它是唯一一条时只剥最后一句；
    整条就是一句问句则留着（可能是必要的反问）。"""
    if not bubbles:
        return bubbles
    last = bubbles[-1].strip()
    if not last.endswith(("?", "？")) or not _CLOSING.search(last):
        return bubbles
    if len(bubbles) > 1:
        return bubbles[:-1]
    sents = _split_sentences(last)
    if len(sents) <= 1:
        return bubbles
    return ["".join(sents[:-1]).strip()]


def _opener(s: str, n: int = 8) -> str:
    return _WS.sub("", s or "")[:n]


def _ngrams(s: str, n: int = 4) -> set:
    t = _WS.sub("", s or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def repeats(bubble: str, recent: Iterable[str], threshold: float = 0.5) -> Optional[str]:
    """新气泡和最近某条开头 8 字相同，或 4-gram Jaccard 超阈值，返回撞上的那条；否则 None。"""
    o = _opener(bubble)
    g = _ngrams(bubble)
    for r in recent:
        if o and _opener(r) == o:
            return r
        gr = _ngrams(r)
        if g and gr:
            j = len(g & gr) / len(g | gr)
            if j > threshold:
                return r
    return None


def truncate_to(bubbles: List[str], limit: int) -> List[str]:
    """按预算截断：整条放得下就留，放不下的那条按句子截，一句都放不下就硬切。"""
    out, used = [], 0
    for b in bubbles:
        n = text_len(b)
        if used + n <= limit:
            out.append(b)
            used += n
            continue
        room = limit - used
        if room <= 0:
            break
        # Only try to cut if this is the first item (nothing added yet)
        if out:
            break
        kept = ""
        for s in _split_sentences(b):
            if text_len(kept + s) <= room:
                kept += s
            else:
                break
        if not kept:
            compact = _WS.sub("", b)
            kept = compact[:room]
        out.append(kept.strip())
        break
    return out
