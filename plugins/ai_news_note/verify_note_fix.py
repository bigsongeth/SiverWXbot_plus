# -*- coding: utf-8 -*-
"""验证 2026-08-01 的三项加固改动，只建笔记、不发送。

验的是 sender.py 改后的这三件事：
  1. _close_all_editors() 关不干净会返回失败（而不是闷头往下走）
  2. _create_note_from_clipboard 只认「点击后新冒出来」的编辑器窗口
  3. 落点按正文 DocumentControl 实时取中心，日志里能看到 rect 和落点

跑法（会话 2）：python plugins/ai_news_note/verify_note_fix.py
副作用：在收藏里留下一条今日日报笔记（不发送到任何群）。输出 panel_logs/verify_note_fix.log。
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "verify_note_fix.log")


def w(line=""):
    s = str(line)
    try:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass
    try:
        print(s)
    except Exception:
        pass


def main():
    try:
        open(OUT, "w", encoding="utf-8").close()
    except Exception:
        pass
    w(f"# verify_note_fix @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    from plugins.ai_news_note import sender as S
    from plugins.ai_news_note import config
    from plugins.ai_news_note.render import render

    ok, why = S._desktop_usable()
    w(f"桌面可用: {ok} {why}")
    if not ok:
        return

    # --- 1. 新增的 _list_editors / _close_all_editors 校验 ---
    w(f"当前编辑器窗口: {sorted(S._list_editors())}")
    ok, why = S._close_all_editors()
    w(f"_close_all_editors() -> ok={ok} msg={why!r}")
    w(f"清理后编辑器窗口: {sorted(S._list_editors())}")
    if not ok:
        w("!! 关不干净，按新逻辑生产会在这里中止（这正是期望行为）")
        return

    # --- 2/3. 走一遍真实建笔记（不发送）---
    with open(config.DATA_FILE, encoding="utf-8") as f:
        d = json.load(f)
    title, frag, hits = render(d)
    cf_bytes = S._build_cf_html(frag)
    expect = title.replace("🐶", "").strip()
    w(f"真实日报：{title!r} CF_HTML={len(cf_bytes)}B")

    S._clip_html(cf_bytes, title)
    ok, msg = S._create_note_from_clipboard(cf_bytes, title, expect)
    w(f"_create_note_from_clipboard -> ok={ok} msg={msg!r}")

    if ok:
        cell = S._find_note_cell(expect)
        w(f"收藏里能找到这条笔记: {bool(cell)}")
        w("✅ 建笔记链路走通（未发送到任何群）")
    else:
        w("❌ 建笔记失败，改动没解决问题")

    S._drop_topmost()
    w("done.")


if __name__ == "__main__":
    main()
