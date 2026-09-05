# -*- coding: utf-8 -*-
"""wx_reply 的接受/退回决定。attempt=1 退回让模型改，attempt>=2 强制收口（截断/放行）。"""
from __future__ import annotations
from typing import List, Optional, Tuple

from . import shape


def validate_reply(bubbles: List[str], is_group: bool, budget: int, recent: List[str],
                   attempt: int, cfg: dict) -> Tuple[Optional[List[str]], Optional[str]]:
    bubbles = [shape.strip_markdown(b).strip() for b in (bubbles or []) if isinstance(b, str)]
    bubbles = [b for b in bubbles if b]
    if not bubbles:
        return None, "bubbles 为空。要么给至少一条有内容的话，要么调用 no_reply。"
    mb = shape.max_bubbles(is_group, cfg)
    if len(bubbles) > mb:
        return None, f"最多 {mb} 条气泡，你给了 {len(bubbles)} 条。合并或删掉最不重要的。"
    total = sum(shape.text_len(b) for b in bubbles)
    if total > budget:
        if attempt < 2:
            return None, f"总字数 {total} 超过预算 {budget}。压到 {budget} 字以内，只留最有信息量的话，去掉铺垫和客套。"
        bubbles = shape.truncate_to(bubbles, budget)
    if attempt < 2:
        for b in bubbles:
            hit = shape.repeats(b, recent, cfg.get("repeat_threshold", 0.5))
            if hit:
                return None, f"「{b[:12]}…」和你之前说过的「{hit[:12]}…」开头或措辞重复了。换个说法，别用同一个开场白。"
    bubbles = shape.strip_closing(bubbles)
    return bubbles, None
