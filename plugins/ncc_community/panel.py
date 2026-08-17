# -*- coding: utf-8 -*-
"""面板 `/ncc_community` 的逻辑层（PANEL_SPEC.md §3）。

**刻意不 import flask、也不 import wxbot_core**：web_server.py 那边只做
"收请求 → 调这里 → jsonify"，所有判断和写盘都在这个纯 Python 模块里。
好处有二：① 面板逻辑能在 mac 上裸跑单测（wxautox 是 Windows 专属）；
② 上游改 web_server.py 时冲突面只有那几行薄路由。

写操作统一走 registry 的 CRUD（都在 registry._LOCK 内），面板与 bot 同进程，
共享同一把锁，不存在跨进程竞争。registry.py 无 wxautox 依赖，
所以**机器人没启动时这个页面照样能用**。
"""
from __future__ import annotations

import os
from datetime import datetime

# task_runner 只 import 了 store/common/wxlock，都不碰 wxautox，
# 所以在这儿导入不会破坏"mac 上能裸跑单测"这条（forward 是在它函数内部才导的）。
from . import registry, store, task_runner

# 「后台」指令回给管理群的地址。Tailscale IP 不随局域网换段变（CLAUDE.md 3.1），
# 端口跟面板一致（10001）。可用 config.json 的 panel_url 覆盖。
DEFAULT_PANEL_URL = "http://100.73.185.46:10001/ncc_community"


def panel_url() -> str:
    return str(store.load().get("panel_url") or DEFAULT_PANEL_URL).strip()


# ---------------------------------------------------------------- 读

def _group_view(name: str, g: dict, known_groupings: set) -> dict:
    """一个群在面板上的展示形态。多带两个诊断位：
    - stale_groupings：身上挂着已被删掉的分组名（历史脏数据，界面上标出来）
    - addressing：转发实际会用的候选串顺序，排查"群不存在"时一眼能看到。"""
    gs = list(g.get("groupings") or [])
    return {
        "name": name,
        "gid": g.get("gid") or "",
        "groupings": gs,
        "stale_groupings": [x for x in gs if x not in known_groupings],
        "allow_forward": bool(g.get("allow_forward")),
        "allow_speak": bool(g.get("allow_speak")),
        "welcome_url": g.get("welcome_url") or "",
        "remark": g.get("remark") or "",
        "remark_applied": bool(g.get("remark_applied")),
        "addressing_hit": g.get("addressing_hit") or "",
        "addressing": registry.address_candidates(g),
        "status": g.get("status") or "active",
        "last_seen": g.get("last_seen") or "",
    }


def state() -> dict:
    """面板一次拿全所有数据（前端不做增量，改完整页重载，简单不出错）。

    顺手把缺 gid 的老条目补上并落盘——一期迁移脚本会做一次，这里兜住
    "迁移前就已存在、迁移后又新发现"的群。"""
    with registry._LOCK:
        data = registry.load()
        if registry.ensure_gids(data):
            registry.save(data)

    groupings_raw = data.get("groupings", {}) or {}
    known = set(groupings_raw)
    groups = data.get("groups", {}) or {}

    used = {}
    for g in groups.values():
        for x in (g.get("groupings") or []):
            used[x] = used.get(x, 0) + 1
    fwd = {}
    for g in groups.values():
        if g.get("allow_forward"):
            for x in (g.get("groupings") or []):
                fwd[x] = fwd.get(x, 0) + 1

    groupings = [{
        "name": n,
        "number": info.get("number"),
        "forward_enabled": bool(info.get("forward_enabled", True)),
        "group_count": used.get(n, 0),
        "forward_count": fwd.get(n, 0),
    } for n, info in groupings_raw.items()]
    # 有编号的按编号，没编号的排最后按名字——跟群里那个选择菜单同序，好对照
    groupings.sort(key=lambda x: (x["number"] is None, x["number"] or 0, x["name"]))

    views = [_group_view(n, g, known) for n, g in sorted(groups.items())]
    invites = [{"keyword": kw, "group": e["group"], "enabled": e["enabled"],
                "valid": e["group"] in groups}
               for kw, e in registry.invite_items(data)]

    return {
        "groupings": groupings,
        "groups": [v for v in views if v["status"] != "pending"],
        "pending": [v for v in views if v["status"] == "pending"],
        "invite_keywords": invites,
        "updated_at": data.get("synced_at") or "",
        "stats": {
            "groups": len(views),
            "forwardable": sum(1 for v in views if v["allow_forward"]),
            "unreachable": sum(1 for v in views if v["status"] == "unreachable"),
            "pending": sum(1 for v in views if v["status"] == "pending"),
            "groupings": len(groupings),
            "keywords": len(invites),
            "keywords_on": sum(1 for i in invites if i["enabled"] and i["valid"]),
        },
    }


# ---------------------------------------------------------------- 写

def _s(payload: dict, key: str, default: str = "") -> str:
    return str(payload.get(key, default) or "").strip()


def _b(payload: dict, key: str) -> bool:
    return bool(payload.get(key))


def _groupings_arg(payload: dict):
    v = payload.get("groupings")
    return v if isinstance(v, list) else []


def _op_group_save(p):
    name = _s(p, "name")
    registry.set_group_fields(
        name,
        groupings=_groupings_arg(p),
        allow_forward=_b(p, "allow_forward"),
        allow_speak=_b(p, "allow_speak"),
        welcome_url=_s(p, "welcome_url"),
    )
    return f"「{name}」已保存"


def _op_group_add(p):
    name = _s(p, "name")
    registry.add_group(name)
    return f"已新增群「{name}」，记得选分组、勾允许转发"


def _op_group_delete(p):
    name = _s(p, "name")
    registry.delete_group(name)
    return f"已从登记表删除「{name}」（指向它的拉群关键词一并清掉）"


def _op_group_rename(p):
    old, new = _s(p, "name"), _s(p, "new_name")
    registry.rename_group(old, new)
    return (f"已改名：「{old}」→「{new}」。"
            f"打过🐶备注的群寻址仍走原备注，不影响转发。")


def _op_group_restore(p):
    name = _s(p, "name")
    registry.restore_reachable(name)
    return f"「{name}」已恢复为可转发，下轮群发会包含它"


def _op_group_classify(p):
    name = _s(p, "name")
    registry.classify_pending(
        name,
        groupings=_groupings_arg(p),
        allow_forward=_b(p, "allow_forward"),
        allow_speak=_b(p, "allow_speak"),
        welcome_url=_s(p, "welcome_url"),
    )
    return f"「{name}」已归类"


def _op_grouping_save(p):
    name = _s(p, "name")
    old = _s(p, "old_name")
    if old and old != name:
        registry.rename_grouping(old, name)
    registry.set_grouping(name, number=p.get("number"),
                          forward_enabled=_b(p, "forward_enabled"))
    return f"分组「{name}」已保存"


def _op_grouping_delete(p):
    name = _s(p, "name")
    registry.delete_grouping(name)
    return f"分组「{name}」已删除（已从所有群身上摘掉）"


def _op_invite_save(p):
    kw, old = _s(p, "keyword"), _s(p, "old_keyword")
    if old and old != kw:
        registry.delete_invite_keyword(old)
    registry.set_invite_keyword(kw, _s(p, "group"),
                                enabled=bool(p.get("enabled", True)))
    return f"拉群关键词「{kw}」已保存（下一条私聊消息即生效，无需重启）"


def _op_invite_delete(p):
    kw = _s(p, "keyword")
    registry.delete_invite_keyword(kw)
    return f"拉群关键词「{kw}」已删除"


# ---------------------------------------------------------------- 体检任务

# ★ 为什么体检指令要搬到面板（2026-08-15）：
# 这些指令要动微信 UI，必须在 bot 进程内跑，所以当初只能"人在管理群里发一句话"。
# 但主菜单状态会把文本吃掉（发「检查群组 全部」回你"请输入有效的选项"），
# 而且指令串全靠手打、错一个字或多个 BOM 就静默不认。
# task_runner 本来就是为"不用真有人在群里发"造的 —— 面板按钮写请求文件，
# bot 每 10 秒取一次，结果写回结果文件，这里只管下发和回读。
TASK_COMMANDS = [
    {"id": "check_groups", "cmd": "检查群组 全部", "label": "检查群组",
     "hint": "逐个切过去确认群还在不在，顺便学微信里的真实显示名（转发靠它寻址）",
     "cost": "87 个群，约 5–10 分钟", "danger": False},
    {"id": "audit_remarks", "cmd": "核对备注 全部", "label": "核对备注",
     "hint": "查🐶备注有没有打到别的群头上 —— 这类错「检查群组」查不出来",
     "cost": "约 5–10 分钟", "danger": False},
    {"id": "fix_addressing", "cmd": "查寻址 全部", "label": "查寻址",
     "hint": "用微信搜索抄回每个群的实际显示名，比逐个切窗口快",
     "cost": "约 10 分钟", "danger": False},
    {"id": "scan_groups", "cmd": "扫群", "label": "扫群",
     "hint": "微信里到底有多少个群（只读，不改任何东西）",
     "cost": "十几秒", "danger": False},
    {"id": "find_unmarked", "cmd": "查新群", "label": "查新群",
     "hint": "挑出没打🐶标签的群 —— 新建的群用它",
     "cost": "十几秒", "danger": False},
    {"id": "fix_remarks_preview", "cmd": "修备注 预览", "label": "修备注（预览）",
     "hint": "只列出要改什么，不动微信。不可逆操作永远先跑预览",
     "cost": "约 5 分钟", "danger": False},
    {"id": "fix_remarks_apply", "cmd": "修备注 全部", "label": "修备注（真改）",
     "hint": "把每个群的备注改成「群名🐶」。★ 备注不可逆：打错只能人在微信里手动清",
     "cost": "约 10 分钟", "danger": True},
]

_TASK_BY_ID = {t["id"]: t for t in TASK_COMMANDS}


def task_catalog() -> list:
    """给前端渲染按钮用（不含真实指令串——指令串在服务端查表，前端拼不出花样）。"""
    return [{k: v for k, v in t.items() if k != "cmd"} for t in TASK_COMMANDS]


def run_task(task_id: str) -> str:
    """把一条体检指令下发给 bot 进程。"""
    task = _TASK_BY_ID.get(str(task_id or ""))
    if task is None:
        raise ValueError(f"未知的体检指令：{task_id}")
    if os.path.exists(task_runner.REQUEST_PATH):
        raise ValueError("上一条指令还没被机器人取走（每 10 秒取一次），稍等几秒再点。")
    status = task_status()
    if status["running"]:
        raise ValueError("已经有一个任务在跑了，等它结束再点 —— 同时跑两个会互相抢微信窗口。")
    os.makedirs(task_runner.DATA_DIR, exist_ok=True)
    # newline="" + utf-8 无 BOM：BOM 会让指令头上多个不可见字符，bot 那边死活匹配不上
    with open(task_runner.REQUEST_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(task["cmd"])
    return f"已下发「{task['cmd']}」，机器人 10 秒内开始，结果实时显示在下面。"


def task_status() -> dict:
    """当前体检任务的状态 + 结果全文（面板轮询这个）。"""
    pending = os.path.exists(task_runner.REQUEST_PATH)
    text, updated = "", None
    if os.path.exists(task_runner.RESULT_PATH):
        try:
            with open(task_runner.RESULT_PATH, "r", encoding="utf-8") as f:
                text = f.read()
            updated = datetime.fromtimestamp(
                os.path.getmtime(task_runner.RESULT_PATH)).strftime("%Y-%m-%d %H:%M:%S")
        except OSError as e:
            text = f"（读结果文件失败：{e}）"
    # 结束行由 task_runner._run 的 finally 写，有它才算跑完
    running = bool(text) and "=== 任务结束" not in text
    return {"pending": pending, "running": running, "result": text,
            "updated_at": updated, "catalog": task_catalog()}


def _op_task_run(p):
    return run_task(_s(p, "task"))


_OPS = {
    "task.run": _op_task_run,
    "group.save": _op_group_save,
    "group.add": _op_group_add,
    "group.delete": _op_group_delete,
    "group.rename": _op_group_rename,
    "group.restore": _op_group_restore,
    "group.classify": _op_group_classify,
    "grouping.save": _op_grouping_save,
    "grouping.delete": _op_grouping_delete,
    "invite.save": _op_invite_save,
    "invite.delete": _op_invite_delete,
}


def apply(op: str, payload: dict | None = None) -> str:
    """执行一次面板操作，返回给用户看的中文提示。

    参数不合法时 registry 层抛 ValueError（中文），由路由转成错误提示——
    **不要在这里 except 掉**：静默吞掉的保存会让人以为改成功了。"""
    fn = _OPS.get(str(op or ""))
    if fn is None:
        raise ValueError(f"未知操作：{op}")
    return fn(payload or {})
