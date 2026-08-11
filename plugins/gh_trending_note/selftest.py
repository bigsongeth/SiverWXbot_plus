# -*- coding: utf-8 -*-
r"""离线自检：只做 import 和渲染，不碰微信 UI。

在项目根目录跑：  python plugins\gh_trending_note\selftest.py
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ok = True


def check(label, fn):
    global ok
    try:
        r = fn()
        print("[OK]   {} {}".format(label, r if r is not None else ""))
        return r
    except Exception as e:
        ok = False
        import traceback
        print("[FAIL] {} -> {!r}".format(label, e))
        traceback.print_exc()
        return None


print("=" * 60)
print("gh_trending_note 离线自检")
print("=" * 60)

cfg = check("import config", lambda: __import__(
    "plugins.gh_trending_note.config", fromlist=["config"]))
check("  配置值", lambda: "enabled={} time={} target={}".format(
    cfg.ENABLED, cfg.SEND_TIME, cfg.TARGET))

check("import render", lambda: __import__(
    "plugins.gh_trending_note.render", fromlist=["render"]))

# 这一步最容易炸：ai_news_note.sender 里的私有函数名如果对不上就在这里暴露
check("import sender（含复用 ai_news_note 的 UIA 原语）", lambda: __import__(
    "plugins.gh_trending_note.sender", fromlist=["sender"]))
check("import trigger", lambda: __import__(
    "plugins.gh_trending_note.trigger", fromlist=["trigger"]))
check("import __init__.register_trending_note", lambda: __import__(
    "plugins.gh_trending_note", fromlist=["register_trending_note"]).register_trending_note)

# 敏感词表是否真的复用上了（复用失败会静默降级为不过滤）
def _sens():
    from plugins.gh_trending_note import render as R
    from plugins.ai_news_note.sensitive import sanitize as real
    return "已复用 ai_news_note 词表" if R.sanitize is real else "⚠️ 降级为不过滤（词表没 import 上）"
check("敏感词表", _sens)

print("-" * 60)

if not os.path.exists(cfg.DATA_FILE):
    print("[FAIL] 数据文件不存在：{}".format(cfg.DATA_FILE))
    ok = False
else:
    with io.open(cfg.DATA_FILE, encoding="utf-8") as f:
        d = json.load(f)
    print("[OK]   数据文件 date={} model={} sections={}".format(
        d.get("date"), d.get("model"),
        [(s.get("since"), len(s.get("items", []))) for s in d.get("sections", [])]))

    from plugins.gh_trending_note.render import render, count_items
    title, frag, hits = render(d)
    print("[OK]   渲染成功 title={!r}".format(title))
    print("[OK]   项目数={} HTML长度={} 敏感词命中={}".format(count_items(d), len(frag), hits or "无"))

    # 选笔记用的 keyword，必须含日期且非空——为空会在收藏里匹配到任意笔记
    kw = title.replace(u"\U0001F4C8", "").strip()
    print("[OK]   选笔记关键词={!r}".format(kw))
    if not kw or len(kw) < 6:
        print("[FAIL] 关键词太短，有发错笔记的风险")
        ok = False

    from plugins.gh_trending_note.sender import _build_cf_html
    cf = _build_cf_html(frag)
    print("[OK]   CF_HTML 构造成功，{} 字节".format(len(cf)))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "preview.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(frag)
    print("[OK]   预览已写出：{}".format(out))

print("=" * 60)
print("自检结果：{}".format("全部通过 ✅" if ok else "有失败项 ❌"))
sys.exit(0 if ok else 1)
