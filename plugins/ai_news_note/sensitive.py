# -*- coding: utf-8 -*-
"""日报内容合规审查：命中敏感词就打码，避免发到微信群被删/封号。

词表 = 内置默认（毒品/违禁品/赌博/军火等微信易触发审核的）+ sensitive_words.txt（用户可增删，一行一词，# 注释）。
白名单避免明显误杀（如"大麻烦"里的"大麻"）。命中的词替换成等长 ● 打码，并返回命中列表（供日志/通知）。
"""
import os

_WORDS_FILE = os.path.join(os.path.dirname(__file__), "sensitive_words.txt")
_MASK = "●"  # 打码字符

# 内置默认高危词（保守精选：明确违禁、AI 日报里一旦出现基本就是真敏感）。
# 政治类不内置（误杀风险高且难判定），需要就自己加到 sensitive_words.txt。
_DEFAULT = [
    # 毒品 / 成瘾物质
    "大麻", "冰毒", "海洛因", "可卡因", "摇头丸", "鸦片", "吗啡", "K粉", "麻古",
    "氯胺酮", "甲基苯丙胺", "罂粟", "制毒", "贩毒", "吸毒", "毒品交易",
    # 赌博
    "赌博", "博彩", "六合彩", "网赌", "赌场",
    # 军火 / 爆炸物
    "枪支", "军火", "炸药", "雷管", "爆炸物",
]

# 白名单：含敏感子串但无害的词，命中先保护、不打码
_WHITELIST = ["大麻烦", "大麻花", "大麻子", "赌场如战场", "枪支弹药库存管理"]


def _load_words():
    words = list(_DEFAULT)
    try:
        with open(_WORDS_FILE, encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if w and not w.startswith("#"):
                    words.append(w)
    except Exception:
        pass
    # 长词优先匹配，避免短词先命中导致长词漏判
    return sorted(set(words), key=len, reverse=True)


def sanitize(text):
    """把 text 里命中的敏感词打码为等长 ●。返回 (clean_text, hit_words)。"""
    if not text:
        return text, []
    words = _load_words()
    hits = []
    # 先把白名单词换成占位符，保护它不被打码
    holders = {}
    for i, wl in enumerate(_WHITELIST):
        if wl in text:
            ph = f"\x00{i}\x00"
            holders[ph] = wl
            text = text.replace(wl, ph)
    for w in words:
        if w and w in text:
            hits.append(w)
            text = text.replace(w, _MASK * len(w))
    for ph, wl in holders.items():
        text = text.replace(ph, wl)
    return text, hits
