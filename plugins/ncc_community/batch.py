# -*- coding: utf-8 -*-
"""Phase 3 批量纳管 —— 给存量群逐个打🐶备注，再回写 Notion。

清单来源：registry（Notion『群聊列表』同步下来的 86 个群，全是群、无个人）。
比"滚动聊天列表"更全、且不会误判个人。

两步（对齐用户："打完标记之后把它同步到 database"）：
  ① run_remark_pass  逐群 ChatWith→确认是群→SetGroupRemark(原名🐶)→登记表标记
  ② run_notion_pass  把已标记群的 Notion『群名』改成「原名🐶」，让表里可见

安全：
- 全程在机器人进程内、持 forward.MAIN_WINDOW_LOCK（与监听/转发同一把锁，
  逐群加锁+释放，让收消息插空），避免独立脚本抢窗口（今晚踩过的坑）；
- 幂等：只处理 remark_applied=False 的群，SetGroupRemark 永不重设（避免追加坑）；
- 防风控：群间随机延迟；支持 limit 先小批试跑；
- 确认是群才设置（ChatInfo.chat_type=='group'），个人/异常跳过并汇报。
"""
from __future__ import annotations

import random
import threading
import time

from . import registry, remark, notion_sync
from .common import REPLY_PREFIX, log

DOG = "\U0001f436"  # 🐶

_RUNNING = threading.Lock()   # 防止重复触发
DELAY_MIN, DELAY_MAX = 3.0, 5.0


# ---------------------------------------------------------------- 预览（不碰微信）

def preview() -> dict:
    data = registry.load()
    todo, done = [], []
    for name, g in data.get("groups", {}).items():
        (done if g.get("remark_applied") else todo).append(name)
    return {"todo": todo, "done": done,
            "todo_n": len(todo), "done_n": len(done), "total": len(todo) + len(done)}


def format_preview() -> str:
    p = preview()
    if p["total"] == 0:
        return "登记表是空的，先在管理群发「同步」从 Notion 拉取。"
    lines = [f"批量纳管预览：共 {p['total']} 群，已打🐶 {p['done_n']}，待打 {p['todo_n']}。"]
    if p["todo"]:
        show = p["todo"][:15]
        lines.append("待打清单（前 %d 个）：" % len(show))
        lines.extend(f"  - {n}" for n in show)
        if p["todo_n"] > len(show):
            lines.append(f"  … 还有 {p['todo_n'] - len(show)} 个")
        lines.append("发「批量备注 5」先试 5 个，或「批量备注」全量。")
    else:
        lines.append("全部已打🐶。发「回写Notion」把表里群名同步成「原名🐶」。")
    return "\n".join(lines)


# ---------------------------------------------------------------- ① 打备注（碰微信）

def run_remark_pass(bot, admin: str, limit: int = 0) -> None:
    """后台线程：给待打群逐个打🐶。limit>0 时只处理前 limit 个。"""
    if not _RUNNING.acquire(blocking=False):
        _send(bot, admin, "已有批量任务在跑，等它结束。")
        return
    threading.Thread(target=_remark_worker, args=(bot, admin, limit), daemon=True).start()


def _remark_worker(bot, admin, limit):
    try:
        from .forward import MAIN_WINDOW_LOCK
        todo = preview()["todo"]
        if limit and limit > 0:
            todo = todo[:limit]
        if not todo:
            _send(bot, admin, "没有待打🐶的群（都打过了）。")
            return
        _send(bot, admin, f"开始批量打🐶备注：{len(todo)} 个群，逐个来，完成后汇报。")
        ok, skip, fail = [], [], []
        for i, name in enumerate(todo, 1):
            status, info = _apply_one(bot.wx, name, MAIN_WINDOW_LOCK)
            if status == "ok":
                ok.append(name)
            elif status == "skip":
                skip.append((name, info))
            else:
                fail.append((name, info))
                log("WARNING", f"批量打备注失败 {name}: {info}")
            if i % 10 == 0:
                _send(bot, admin, f"进度 {i}/{len(todo)}：成功 {len(ok)} 跳过 {len(skip)} 失败 {len(fail)}")
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        lines = [f"{REPLY_PREFIX} 批量打🐶完成：成功 {len(ok)}、跳过 {len(skip)}、失败 {len(fail)}。"]
        if skip:
            lines.append("跳过（非群/异常）：" + "、".join(f"{n}({r})" for n, r in skip[:8]))
        if fail:
            lines.append("失败：" + "、".join(f"{n}({r})" for n, r in fail[:8]))
        if ok:
            lines.append("下一步：发「回写Notion」把表里群名同步成「原名🐶」。")
        _send(bot, admin, "\n".join(lines))
    except Exception as e:
        log("ERROR", f"批量打备注线程异常: {e}")
        _send(bot, admin, f"批量打备注出错：{e}")
    finally:
        _RUNNING.release()


def _apply_one(wx, name, lock):
    """给单个群打🐶：确认窗口就是该群→SetGroupRemark→登记表标记。返回 (status, info)。

    确认不只看 chat_type：模糊搜索可能命中名字相近的另一个群，备注打错清不掉。"""
    data = registry.load()
    g = registry.get_group(data, name)
    if g and g.get("remark_applied"):
        return "ok", "已打过"
    try:
        with lock:
            wx.ChatWith(name, exact=False)
            ok, why = remark.confirm_group_window(wx, name)
            if not ok:
                return "skip", why
            r = wx.SetGroupRemark(name + DOG)
    except Exception as e:
        return "fail", str(e)
    if remark.wxresponse_ok(r):
        registry.mark_remark_applied(name, name + DOG)
        log("INFO", f"批量打🐶：{name}")
        return "ok", name + DOG
    return "fail", f"SetGroupRemark 返回 {r!r}"


# ---------------------------------------------------------------- ② 回写 Notion 标题

def run_notion_pass(bot, admin: str) -> None:
    if not _RUNNING.acquire(blocking=False):
        _send(bot, admin, "已有批量任务在跑，等它结束。")
        return
    threading.Thread(target=_notion_worker, args=(bot, admin), daemon=True).start()


def _notion_worker(bot, admin):
    try:
        data = registry.load()
        # 已打🐶、有 Notion page、标题还没带🐶的群
        todo = [(name, g) for name, g in data.get("groups", {}).items()
                if g.get("remark_applied") and g.get("notion_page_id")]
        if not todo:
            _send(bot, admin, "没有需要回写的群（可能还没打🐶，或没同步过 Notion）。")
            return
        _send(bot, admin, f"开始回写 Notion 群名（{len(todo)} 个）…")
        ok = fail = 0
        for name, g in todo:
            try:
                notion_sync.update_title_dog(g["notion_page_id"], name)
                ok += 1
            except Exception as e:
                fail += 1
                log("WARNING", f"回写 Notion 失败 {name}: {e}")
            time.sleep(0.4)  # Notion 限流友好
        _send(bot, admin, f"{REPLY_PREFIX} Notion 回写完成：成功 {ok}、失败 {fail}。"
                          f"表里群名已带🐶，群名再改也锁得住了。")
    except Exception as e:
        log("ERROR", f"Notion 回写线程异常: {e}")
        _send(bot, admin, f"回写 Notion 出错：{e}")
    finally:
        _RUNNING.release()


# ---------------------------------------------------------------- 指令入口

def handle_batch_command(bot, chat, cfg, text) -> bool:
    """管理群里的批量纳管指令。命中返回 True。"""
    admin = cfg.get("admin_group")
    t = text.strip()
    plain = t.replace(" ", "")

    if plain in ("批量备注预览", "纳管预览", "预览纳管"):
        _send_chat(chat, format_preview()); return True
    if plain in ("批量备注", "批量纳管", "纳管全部"):
        run_remark_pass(bot, admin, limit=0); return True
    import re
    m = re.match(r"^(?:批量备注|批量纳管)\s*(\d+)$", t)
    if m:
        run_remark_pass(bot, admin, limit=int(m.group(1))); return True
    if plain in ("回写notion", "回写Notion", "同步备注到notion", "同步备注到Notion", "回写群名"):
        run_notion_pass(bot, admin); return True
    return False


def _send(bot, admin, text):
    try:
        bot.wx.SendMsg(msg=f"{REPLY_PREFIX} {text}", who=admin)
    except Exception as e:
        log("ERROR", f"批量汇报失败: {e}")


def _send_chat(chat, text):
    try:
        chat.SendMsg(msg=f"{REPLY_PREFIX} {text}")
    except Exception as e:
        log("ERROR", f"批量回复失败: {e}")
