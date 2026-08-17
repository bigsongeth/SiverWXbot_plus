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


def _date_label(d):
    """date 渲染成"8月12日"这种短标签（解析不了就原样用）。"""
    date_str = d.get("date") or "0000-00-00"
    try:
        _, m, day = date_str.split("-")
        return f"{int(m)}月{int(day)}日"
    except Exception:
        return date_str


def render_plain(d, mode="digest"):
    """把 latest.json 渲染成**纯文本块**，供"不新建窗口"的降级通路直接发群用。

    与 render() 同数据、同一套敏感词审查，只是输出纯文本。微信纯文本没有标题层级/高亮/
    列表（见顶部能力边界），排版只靠 emoji 前缀和空行；URL 裸写，微信自己变蓝链。
    mode='digest'：序号+标题 / 评分+来源 / 链接（实测 12 条约 1.4k 字）；
    mode='full'  ：额外带摘要与关键词（实测 12 条约 4.2k 字）。
    返回 (title, blocks, hits)；blocks 每条日报一块，块内绝不能切开（否则 URL 断），
    怎么装进消息由调用方决定（见 sender._pack_blocks）。
    """
    items = sorted(d.get("items", []), key=lambda x: x.get("rank", 999))
    title = f"{config.TITLE_PREFIX} · {_date_label(d)}"
    all_hits = []

    def clean(t):
        """敏感词审查：命中打码，收集命中词（与 render() 同一套词表）。"""
        c, h = sanitize(str(t if t is not None else ""))
        if h:
            all_hits.extend(h)
        return c

    blocks = []
    for it in items:
        url = str(it.get("url") or "").strip()
        lines = [f"{it.get('rank')}. {clean(it.get('title'))}"]
        if mode == "full":
            lines.append(f"📰 {it.get('source')} ｜ ⭐ {it.get('score')}/10 "
                         f"｜ 🕐 {it.get('published_at')}")
            lines.append(clean(it.get("summary")))
            kw = " / ".join(it.get("keywords") or [])
            if kw.strip():
                lines.append(f"🔑 {clean(kw)}")
        else:
            lines.append(f"⭐ {it.get('score')}/10 ｜ {it.get('source')}")
        if url:
            lines.append(f"🔗 {url}")
        blocks.append("\n".join(x for x in lines if str(x).strip()))
    return title, blocks, sorted(set(all_hits))
