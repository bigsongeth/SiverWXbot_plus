# -*- coding: utf-8 -*-
"""把一条微信消息变成喂给大脑的用户消息：前缀行 + 历史（首轮整段预热，之后每轮带上次之后的新消息）+ 正文。"""
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


# 机器人自己的回复在流水里多半是 quote（引用回复），不是 text。
# miniapp/location/link 也要进历史（用户 2026-09-06 拍板）：美食群里群友发的大众点评/美团卡片、位置消息就是要收录的店，
# 只留 text 等于对它们视而不见。图片/语音/表情的 content 只是占位符，不带。
_PRIME_TYPES = ("text", "quote", "miniapp", "location", "link")

# 微信把小程序卡片拼成「小程序 + 小程序名（含一串分类词）+ 卡片标题」，这里剥掉前缀标出类型，模型不用猜哪段是店名。
# 顺序：长前缀在前，短的兜底。
_MINIAPP_APPS = (
    ("大众点评美食电影运动旅游门票", "大众点评卡片"),
    ("大众点评", "大众点评卡片"),
    ("美团外卖丨外卖美食奶茶咖啡水果", "美团外卖卡片"),
    ("美团外卖", "美团外卖卡片"),
    ("美团", "美团卡片"),
)


def render_content(item: dict) -> str:
    """把记忆流水里一条消息的 content 渲染成给模型看的一行（卡片/位置带类型标签，其余原样）。"""
    t = str(item.get("type", "text"))
    c = str(item.get("content", ""))
    if t == "miniapp":
        body = c[len("小程序"):] if c.startswith("小程序") else c
        for prefix, label in _MINIAPP_APPS:
            if body.startswith(prefix):
                return f"[{label}] {body[len(prefix):].strip()}"
        return f"[小程序卡片] {body.strip()}"
    if t == "location":
        body = c[len("位置"):] if c.startswith("位置") else c
        return f"[位置] {body.strip()}"
    if t == "link":
        return f"[链接] {c.strip()}"
    return c


def fingerprint(item: dict) -> tuple:
    """一条流水的身份：记忆文件里的 time 不保证单调（@ 消息是回复时才落盘的），所以不能拿时间当水位线。"""
    return (str(item.get("time", "")), str(item.get("sender", "")), str(item.get("content", "")), str(item.get("type", "text")))


def _basic_prime_filter(items: List[dict]) -> List[dict]:
    return [x for x in items
            if x.get("attr") in ("friend", "self") and x.get("type", "text") in _PRIME_TYPES
            and not is_checkin_text(str(x.get("content", "")))
            and not _is_dropped_self_fallback(x)]


def filter_prime(items: List[dict], count: int, is_group: bool = False) -> List[dict]:
    """私聊走 context_guard 的 filter_history（坏回复整轮连坐）再做基础过滤；
    群聊不走连坐：那条规则按"一问一答"写的，群里两条 self 之间夹着十几条别人的发言，
    连坐会把整段历史清成 0（回放第一轮联邦群 20 条 → 0）。私聊连坐后若一条不剩，
    也退回基础过滤——没历史比带一点脏历史更糟。"""
    raw = list(items or [])
    if is_group:
        kept = _basic_prime_filter(raw)
    else:
        kept = _basic_prime_filter(filter_history(list(raw)))
        if not kept and any(x.get("attr") == "friend" for x in raw):
            kept = _basic_prime_filter(raw)
    return kept[-count:]


def select_new(items: List[dict], seen: set, is_group: bool, exclude: Optional[tuple] = None, count: int = 40) -> List[dict]:
    """会话预热过之后每轮带的增量：机器人这次传来的历史里，模型还没看过的那些（按 fingerprint 判，不按时间）。

    exclude=(发言人, 正文) 是当前这条消息本身——它单独写在正文里，不该在历史里再出现一次
    （调 AI 时它其实还没落盘，这里是双保险）。过滤规则与预热一致（系统条目/签到/兜底文案不进），
    但不走私聊的整轮连坐——那条规则是给"一问一答"写的，对增量没意义。
    """
    ex_sender, ex_text = (exclude or ("", ""))
    ex_text = (ex_text or "").strip()
    out = []
    for x in _basic_prime_filter(list(items or [])):
        if fingerprint(x) in seen:
            continue
        if ex_text and str(x.get("sender", "")) == ex_sender and ex_text in str(x.get("content", "")):
            continue
        out.append(x)
    return out[-count:]


PRIME_LABEL = "以下是此前的聊天记录，只供了解背景，不要逐条回复："
DELTA_LABEL = "以下是上一轮之后群里新出现的消息（含卡片/位置），只供了解背景，不要逐条回复："


def build_user_message(conversation: str, is_group: bool, sender: str, text: str, now_str: str,
                       skills: List[str], prime: Optional[List[dict]] = None, prime_label: str = PRIME_LABEL) -> str:
    kind = "群聊" if is_group else "私聊"
    head = f"[{kind}:{conversation} | 发言人:{sender} | {now_str} | 相关技能:{','.join(skills) if skills else '无'}]"
    parts = [head]
    if prime:
        lines = []
        for x in prime:
            who = f"你({x.get('sender')})" if x.get("attr") == "self" else x.get("sender")
            lines.append(f"[{x.get('time')}] {who}: {render_content(x)}")
        parts.append(prime_label + "\n" + "\n".join(lines) + "\n---")
    parts.append(f"{sender}: {text}")
    return "\n".join(parts)
