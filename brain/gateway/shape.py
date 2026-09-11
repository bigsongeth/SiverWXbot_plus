# -*- coding: utf-8 -*-
"""回复形状的纯函数。不 import 任何网关状态，mac 上裸跑单测。

规则来自设计文档 §4.4：长度预算、剥 Markdown、剥收尾套话、反口头禅、截断。
"""
from __future__ import annotations
import os
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


# ---------------------------------------------------------------- 维度 A：办事 vs 闲聊

_KW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "errand_keywords.txt")

# 问句形态。长尾技术/事务问题靠它兜住，不必把词表堆成穷举。
_QUESTION = re.compile(
    r"(怎么|咋办|咋弄|如何|多少|在哪|哪里|哪儿|哪个|能不能|可不可以|有没有|是不是"
    r"|什么时候|什么价|啥价|贵不贵|怎么样|咋样|为什么|为啥|求推荐|请教|帮我看"
    r"|解释|介绍|分析|查查|查一下|看看有|推荐|\?|？)")


def load_errand_keywords(path: Optional[str] = None) -> tuple:
    """读事务关键词表。返回 tuple 方便当缓存键；文件缺失返回空 tuple（全判闲聊，等于退回旧行为的保守侧）。"""
    try:
        with open(path or _KW_PATH, encoding="utf-8") as f:
            words = [ln.strip() for ln in f]
    except OSError:
        return ()
    return tuple(w.lower() for w in words if w and not w.startswith("#"))


def errand_hits(text: str, keywords: Iterable[str]) -> List[str]:
    """命中的事务关键词。大小写不敏感、按「包含」匹配（与 kb_keywords 的语义一致）。"""
    t = (text or "").lower()
    return [w for w in keywords if w in t]


# 招呼/寒暄：短、且不含任何实词。「在吗？」「你好？」光凭一个问号不该当成办事。
_GREETING_MAX = 6


def is_errand(text: str, keywords: Iterable[str]) -> bool:
    """这轮是不是「办事」。命中关键词、或长得像问句且不是纯寒暄，都算。

    宁可宽松：误判成办事只是多给点额度；判漏则据点/价格/报名答不全（CLAUDE.md 3.7 的事故类型）。
    """
    if errand_hits(text, keywords):
        return True
    if text_len(text) <= _GREETING_MAX:
        return False        # 「在吗？」「你好？」——短又没有实词，是打招呼不是办事
    return bool(_QUESTION.search(text or ""))


# ---------------------------------------------------------------- 维度 B：对方想不想深入

# 显式的「多说点」信号。
# ⚠️ 「为什么/为啥」刻意不在这里 —— 日常问句里太常见（「为啥吃寿司郎要排队呀？」会被判成
# 想深入、拿到群聊顶格 150，而它实际只值 37 字）。问原因算「办事」，深度只认明确的「多说点」。
_DEPTH_WORDS = re.compile(
    r"(展开(说|讲|聊)|详细|具体(说|讲|聊|点)|细说|多说(点|些)|再说说|说说看|讲讲|还有呢|还有吗"
    r"|深入(聊|讲|说)|怎么做到|什么原理|原理是|(有什么|啥)区别|举个例子|详细点|说得再)")


def _friend_history(history: Optional[list], sender: str, current: str, limit: int = 3) -> List[str]:
    """对方最近说过的话（不含当前这条）。history 每条形如 {time,sender,content,type,attr}。"""
    if not history:
        return []
    cur = _WS.sub("", current or "")
    out = []
    for item in reversed(history):
        if not isinstance(item, dict) or item.get("attr") != "friend":
            continue
        if sender and str(item.get("sender", "")) != str(sender):
            continue
        c = str(item.get("content", ""))
        if not c.strip() or _WS.sub("", c) == cur:   # 排除当前这条本身，否则自己跟自己比必然判追问
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def is_followup(text: str, history: Optional[list], sender: str, keywords: Iterable[str]) -> bool:
    """对方是不是在追问同一个话题——最诚实的「我想听更多」信号，而且模型骗不到。

    两条判据（任一成立）：
      1. 与最近某条共享事务关键词（"大理还能去吗" → "大理什么价格"，共享「大理」）；
      2. 2-gram 交集 ≥ 2（措辞重合；中文短句用 4-gram 会退化成空集，故这里用 2）。
    """
    prev = _friend_history(history, sender, text)
    if not prev:
        return False
    kw_now = set(errand_hits(text, keywords))
    g_now = _ngrams(text, 2)
    for p in prev:
        if kw_now and kw_now & set(errand_hits(p, keywords)):
            return True
        if len(g_now & _ngrams(p, 2)) >= 2:
            return True
    return False


def depth_level(text: str, history: Optional[list], sender: str, keywords: Iterable[str],
                long_input: bool = False) -> str:
    """维度 B，分强弱两档。纯粹看对方的措辞和行为，不问模型——模型自评长度会系统性漂向长档。

    - "explicit"：明说要展开（「详细讲讲」「举个例子」）。强信号，对方点名要更多。
    - "followup"：在追问同一话题。**弱**信号 —— 连续问币价也算追问，但那是一串短问题，
      不是想听长回答。回放生产日志：闲聊+追问的实际用量中位只有 26 字，给 ×1.7 会给到 87。
    - long_input：办事类的长提问也按 "explicit" 算（见下方注释）。
    - ""：常规。
    """
    if _DEPTH_WORDS.search(text or ""):
        return "explicit"
    # 认真打了一长段来问正事 = 投入度信号，等同于明说要展开。
    # 回放抓到的：松爸用 111 字问「基于这个剧本我们能做什么」，实际答了 218 字 ——
    # 措辞里没有任何「详细讲讲」，光靠追问的弱加成给到 175 会被裁掉。
    # 只对「办事」生效：闲聊灌一大段（实用 max 只有 52 字）不该跟着放宽。
    if long_input and is_errand(text, keywords):
        return "explicit"
    if is_followup(text, history, sender, keywords):
        return "followup"
    return ""


def wants_depth(text: str, history: Optional[list], sender: str, keywords: Iterable[str]) -> bool:
    """维度 B 的布尔版（是否有任何深度信号）。要区分强弱用 depth_level。"""
    return bool(depth_level(text, history, sender, keywords))


# ---------------------------------------------------------------- 预算

def budget(incoming: str, is_group: bool, cfg: dict,
           history: Optional[list] = None, sender: str = "",
           keywords: Optional[Iterable[str]] = None) -> int:
    """这一轮允许说多少字。

    旧公式 `30 + 2.5×入长` 的自变量选错了：对方说得越短给得越少，于是「展开说说」只有 40 字，
    而灌水一大段闲聊反而顶格 220。新公式按「办事/闲聊 × 想不想深入」两维给，入长只留作弱信号。
    两维都由代码从对方消息里算出，模型不参与 —— 零漂移、零额外推理、零延迟。
    设计见 docs/superpowers/specs/2026-09-11-reply-length-design.md。
    """
    return budget_detail(incoming, is_group, cfg, history, sender, keywords)[0]


def budget_detail(incoming: str, is_group: bool, cfg: dict,
                  history: Optional[list] = None, sender: str = "",
                  keywords: Optional[Iterable[str]] = None) -> tuple:
    """(预算, 判定明细)。明细进 replies 日志，用来回看判得准不准 —— 没有它就没法调参。"""
    b = cfg["budget"]
    cap = b["max_group"] if is_group else b["max_private"]
    n = text_len(incoming)
    if b.get("mode", "two_axis") == "legacy":
        raw = b.get("base", 30) + b.get("factor", 2.5) * n
        return int(max(b["min"], min(cap, raw))), {"mode": "legacy"}
    kws = load_errand_keywords() if keywords is None else tuple(keywords)
    errand = is_errand(incoming, kws)
    level = depth_level(incoming, history, sender, kws, long_input=n >= b.get("long_input_chars", 60))
    base = b["base_errand"] if errand else b["base_chat"]
    mult = {"explicit": b["deep_factor"], "followup": b.get("followup_factor", 1.25)}.get(level, 1.0)
    raw = (base + b["len_factor"] * n) * mult
    val = int(max(b["min"], min(cap, raw)))
    return val, {"mode": "two_axis", "errand": errand, "deep": bool(level), "depth": level,
                 "hits": errand_hits(incoming, kws)[:5], "in_len": n}


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


# ---------------------------------------------------------------- 中文全角标点

# 汉字 + 中文标点 + 全角字符。已转成全角的标点也落在 ＀-￯，所以连续标点能连锁转（「什么?!」→「什么？！」）。
_CJK = re.compile(r"[　-〿一-鿿豈-﫿＀-￯]")
# 句号 `.` 刻意不转：小数点、版本号、英文缩写误伤面太大，而中文句末用半角句号的情况少见。
_FULLWIDTH_MAP = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！", "(": "（", ")": "）"}
_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(_CJK.match(ch))


def to_fullwidth(s: Optional[str]) -> str:
    """把中文语境里的半角标点转成全角。

    起因：2026-09-06 全天真实回复里半角标点 67 个、全角 0 个（「在呢在呢,大半夜滴我,…有事?」）。
    与预算无关 —— text_len 按字符数算，`，` 和 `,` 都是 1，半角并不省字数；纯粹是模型的输出习惯。

    规则：**只转左右任一侧紧邻中文的**那些。代码块与 URL 先挖走再还原。
      「GPT-6:能力惊」→ 右侧中文，转 ✅      「3,000」「v1.2」→ 两侧非中文，不转 ✅
      「https://a.com:8080」→ 在 URL 里，不碰 ✅
    """
    if not s:
        return s or ""
    holes: List[str] = []

    def _stash(m):
        holes.append(m.group(0))
        return "\x00%d\x00" % (len(holes) - 1)

    t = _URL.sub(_stash, _CODE.sub(_stash, s))
    out = list(t)
    for i, ch in enumerate(t):
        rep = _FULLWIDTH_MAP.get(ch)
        if not rep:
            continue
        left = out[i - 1] if i > 0 else ""      # 用 out：左邻若刚被转成全角，这里能接着连锁
        right = t[i + 1] if i + 1 < len(t) else ""
        if _is_cjk(left) or _is_cjk(right):
            out[i] = rep
    t = "".join(out)
    for i, h in enumerate(holes):
        t = t.replace("\x00%d\x00" % i, h)
    return t
