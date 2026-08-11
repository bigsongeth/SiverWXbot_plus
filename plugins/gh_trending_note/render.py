# -*- coding: utf-8 -*-
"""把 gh_trending 的 latest.json 渲染成微信笔记可用的 HTML 片段。

微信笔记（CF_HTML 粘贴）能力边界（沿用 ai_news_note 的实测结论）：
  保留：h1/h2 标题、加粗、斜体、无序列表、明文 URL(自动变可点蓝链)、background-color 背景高亮
  丢失：文字 color、<mark> 标签、blockquote 引用块
所以：高亮一律用 background-color；链接一律用明文 URL，别用 <a>。

版式：日榜详细展开，周榜/月榜精简一行——15 条全详细的话笔记太长，读不完。
"""
import html
from . import config

try:  # 复用 ai_news_note 的敏感词表，避免两套词表各自漂移
    from plugins.ai_news_note.sensitive import sanitize
except Exception:  # 插件缺失时降级为不过滤，不能因此挡住发送
    def sanitize(t):
        return t, []


def esc(s):
    return html.escape(str(s if s is not None else ""))


def render(d):
    """输入 latest.json 解析后的 dict，返回 (title, html_fragment, sensitive_hits)。"""
    date_str = d.get("date") or "0000-00-00"
    try:
        _, m, day = date_str.split("-")
        date_label = "{}月{}日".format(int(m), int(day))
    except Exception:
        date_label = date_str

    title = "{} · {}".format(config.TITLE_PREFIX, date_label)
    all_hits = []

    def clean(t):
        c, h = sanitize(str(t if t is not None else ""))
        if h:
            all_hits.extend(h)
        return c

    parts = [
        "<h1>{}</h1>".format(esc(title)),
        "<p>{}</p>".format(esc(config.INTRO)),
        "<p></p>",
    ]

    for sec in d.get("sections", []):
        items = sorted(sec.get("items", []), key=lambda x: x.get("rank", 999))
        if not items:
            continue
        parts.append("<h1>{}最热 Top{}</h1>".format(esc(sec.get("since_cn", "")), len(items)))
        verbose = sec.get("since") in config.VERBOSE_SECTIONS
        for it in items:
            ai = it.get("ai") or {}
            rel = ai.get("relevance", 0)
            bg = config.HIGHLIGHT_HOT if rel >= config.REL_MARK else config.HIGHLIGHT
            head = "{}. {} +{:,}⭐".format(
                it.get("rank"), it.get("full_name"), it.get("stars_period", 0))

            # 版式（2026-08-09 用户定）：只讲「它是干嘛的」+「为什么推荐」。
            # 不显示相关度分数、不显示风险提醒 —— 那些仍写在 latest.md/json 存档里，
            # 笔记里只留读者真正要看的两句。相关度只用来决定标题底色（绿=更值得看）。
            if verbose:
                parts.append('<h2><span style="background-color:{};">{}</span></h2>'.format(
                    bg, esc(head)))
                parts.append("<p>{}</p>".format(esc(clean(ai.get("what") or it.get("desc")))))
                if ai.get("why"):
                    parts.append("<p>💡 {}</p>".format(esc(clean(ai["why"]))))
                parts.append("<p>{}｜总 {:,}⭐</p>".format(
                    esc(it.get("lang") or "—"), it.get("stars_total", 0)))
                parts.append("<p>🔗 {}</p>".format(esc(it.get("url"))))
                parts.append("<p>─────────────</p>")
            else:
                parts.append('<p><span style="background-color:{};"><b>{}</b></span></p>'.format(
                    bg, esc(head)))
                parts.append("<p>{}</p>".format(esc(clean(ai.get("what") or it.get("desc")))))
                parts.append("<p>🔗 {}</p>".format(esc(it.get("url"))))
        parts.append("<p></p>")

    return title, "".join(parts), sorted(set(all_hits))


def count_items(d):
    return sum(len(s.get("items", [])) for s in d.get("sections", []))
