# -*- coding: utf-8 -*-
"""把 latest.json 渲染成微信笔记可用的 HTML 片段。

微信笔记（CF_HTML 粘贴）能力边界（实测 2026-07-05，微信 4.1.9）：
  保留：h1/h2 标题、加粗、斜体、无序列表、明文 URL(自动变可点蓝链)、background-color 背景高亮
  丢失：文字 color、<mark> 标签、blockquote 引用块
所以：高亮一律用 background-color；链接一律用明文 URL，别用 <a>。

内容合规：发到微信群前，对每条 title/summary/keywords 过敏感词（sensitive.sanitize），命中打码。
"""
import html
from . import config
from .sensitive import sanitize


def esc(s):
    return html.escape(str(s if s is not None else ""))


def render(d):
    """输入 latest.json 解析后的 dict，返回 (title, html_fragment, sensitive_hits)。"""
    items = sorted(d.get("items", []), key=lambda x: x.get("rank", 999))
    date_str = d.get("date") or "0000-00-00"
    try:
        _, m, day = date_str.split("-")
        date_label = f"{int(m)}月{int(day)}日"
    except Exception:
        date_label = date_str

    title = f"{config.TITLE_PREFIX} · {date_label}"

    all_hits = []

    def clean(t):
        """敏感词审查：命中打码，收集命中词。"""
        c, h = sanitize(str(t if t is not None else ""))
        if h:
            all_hits.extend(h)
        return c

    parts = [
        f"<h1>{esc(title)}</h1>",
        f"<p>{esc(config.INTRO)}</p>",
        "<p></p>",
    ]
    for it in items:
        kw = " / ".join(it.get("keywords") or [])
        parts.append(
            f'<h2><span style="background-color:{config.HIGHLIGHT};">'
            f'{esc(it.get("rank"))}. {esc(clean(it.get("title")))}</span></h2>'
        )
        parts.append(
            f"<p>📰 {esc(it.get('source'))} ｜ ⭐ {esc(it.get('score'))}/10 "
            f"｜ 🕐 {esc(it.get('published_at'))}</p>"
        )
        parts.append(f"<p>{esc(clean(it.get('summary')))}</p>")
        parts.append(f"<p>🔑 关键词：{esc(clean(kw))}</p>")
        parts.append(f"<p>🔗 {esc(it.get('url'))}</p>")
        parts.append("<p>─────────────</p>")

    return title, "".join(parts), sorted(set(all_hits))
