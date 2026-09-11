# -*- coding: utf-8 -*-
"""wx_reply 的接受/退回决定。attempt=1 退回让模型改，attempt>=2 强制收口（截断/放行）。"""
from __future__ import annotations
from typing import List, Optional, Tuple

from . import shape


def validate_reply(bubbles: List[str], is_group: bool, budget: int, recent: List[str],
                   attempt: int, cfg: dict) -> Tuple[Optional[List[str]], Optional[str]]:
    budget = max(int(budget), 1)
    bubbles = [shape.strip_markdown(b).strip() for b in (bubbles or []) if isinstance(b, str)]
    if cfg.get("fullwidth_punct", True):
        # 中文语境里的半角标点转全角。跟 strip_markdown 同一道理：模型管不住的事交给确定性后处理。
        bubbles = [shape.to_fullwidth(b) for b in bubbles]
    bubbles = [b for b in bubbles if b]
    if not bubbles:
        return None, "bubbles 为空。要么给至少一条有内容的话，要么调用 no_reply。"
    mb = shape.max_bubbles(is_group, cfg)
    if len(bubbles) > mb:
        return None, f"最多 {mb} 条气泡，你给了 {len(bubbles)} 条。合并或删掉最不重要的。"
    total = sum(shape.text_len(b) for b in bubbles)
    if total > budget:
        # 小幅超预算直接按句子裁掉，不退回重写。生产实测 53% 的轮次在跑第二遍，每次退回都是一整轮
        # LLM 调用 —— 卡死的预算本身就是最大的延迟来源。裁按句子边界（断在半句比啰嗦更毁体验）。
        tol = float(cfg["budget"].get("overflow_tolerance", 1.3))
        if attempt < 2 and total > budget * tol:
            return None, f"总字数 {total} 超过预算 {budget}。压到 {budget} 字以内，只留最有信息量的话，去掉铺垫和客套。"
        bubbles = shape.truncate_to(bubbles, budget)
    if attempt < 2:
        for i, b in enumerate(bubbles):
            hit = shape.repeats(b, recent + bubbles[:i], cfg.get("repeat_threshold", 0.5))
            if hit:
                if i > 0 and hit in bubbles[:i]:
                    earlier_idx = bubbles[:i].index(hit)
                    return None, f"第 {i+1} 条和第 {earlier_idx+1} 条重复了，合并成一条或删掉一条。"
                else:
                    return None, f"「{b[:12]}…」和你之前说过的「{hit[:12]}…」开头或措辞重复了。换个说法，别用同一个开场白。"
    bubbles = shape.strip_closing(bubbles)
    if not bubbles:
        return None, "处理后没有剩下任何内容。要么重新给一条有信息量的话，要么调用 no_reply。"
    return bubbles, None
