# -*- coding: utf-8 -*-
"""本地群登记表（registry.json）—— 引擎的地基。

真相源是 Notion（人维护分组/权限），本模块是同步下来的运行时缓存，
机器人的转发/发现/迎新都读它。数据由 notion_sync 写入、discovery 补充。

数据结构：
{
  "synced_at": "2026-07-07T01:00:00",
  "groupings": {                     # 转发分组（对应 Notion「转发群聊分组」）
    "大理群": {"number": 4, "forward_enabled": true},
    ...
  },
  "groups": {                        # 键 = 群当前名字（对应 Notion「群名」标题）
    "NCC大理共居一家人👪": {
      "name": "NCC大理共居一家人👪",  # 群当前名（寻址用，未打备注前）
      "remark": "NCC大理共居一家人👪🐶", # 目标备注（打上后寻址用这个）
      "remark_applied": false,        # 是否已在微信里打上🐶备注
      "notion_page_id": "…",
      "allow_forward": true,
      "allow_speak": true,
      "welcome_url": "",
      "groupings": ["大理群"],
      "status": "active",             # active=Notion 已归类；pending=新发现未归类
      "last_seen": "2026-07-07T…"
    }
  }
}

寻址主键 target(g) = remark（已打备注）否则 name。这样 86 个存量群在打备注前
仍能按当前名字转发，打上🐶后自动切到按备注寻址（群名再改也锁得住）。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime

DOG = "\U0001f436"  # 🐶

_LOCK = threading.RLock()
_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "registry.json")

_EMPTY = {"synced_at": None, "groupings": {}, "groups": {}, "invite_keywords": {}}


# ---------------------------------------------------------------- 读写

def load() -> dict:
    """读取登记表，文件不存在时返回空结构（不落盘，等首次 sync/discovery 再写）。"""
    with _LOCK:
        if not os.path.exists(REGISTRY_PATH):
            return copy.deepcopy(_EMPTY)
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return copy.deepcopy(_EMPTY)
        data.setdefault("groupings", {})
        data.setdefault("groups", {})
        data.setdefault("invite_keywords", {})
        return data


def save(data: dict) -> None:
    """原子化写入登记表。"""
    with _LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = REGISTRY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, REGISTRY_PATH)


# ---------------------------------------------------------------- 寻址

def target(group: dict) -> str:
    """一个群的寻址字符串：打了🐶备注用备注，否则用当前群名。"""
    if group.get("remark_applied") and group.get("remark"):
        return group["remark"]
    return group.get("name", "")


def match_key(group: dict):
    """返回该群在微信里可能显示的名字集合（用于把 chat.who 匹配到登记表）。"""
    keys = set()
    if group.get("name"):
        keys.add(group["name"])
    if group.get("remark_applied") and group.get("remark"):
        keys.add(group["remark"])
    return keys


# ---------------------------------------------------------------- 查询

def find_by_chat_who(data: dict, chat_who: str):
    """把一个实时 chat.who 匹配到登记表里的群（先精确名/备注，再宽松）。返回 (name, group) 或 (None, None)。"""
    if not chat_who:
        return None, None
    for name, g in data.get("groups", {}).items():
        if chat_who in match_key(g):
            return name, g
    return None, None


def is_known(data: dict, chat_who: str) -> bool:
    name, _ = find_by_chat_who(data, chat_who)
    return name is not None


def list_forward_groupings(data: dict):
    """可用于转发的分组列表（forward_enabled 且至少含 1 个允许转发的群），
    按 Notion 分组编号排序（无编号排最后）。返回 [(grouping_name, target_count)]。"""
    result = []
    for gname, ginfo in data.get("groupings", {}).items():
        if not ginfo.get("forward_enabled", True):
            continue
        cnt = len(targets_for_grouping(data, gname))
        if cnt > 0:
            result.append((gname, ginfo.get("number"), cnt))
    # 有编号的按编号升序，无编号(None)的排最后按名字
    result.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0, x[0]))
    return [(name, cnt) for name, _num, cnt in result]


def targets_for_grouping(data: dict, grouping_name: str):
    """某分组下所有【允许转发】的群的寻址字符串列表。"""
    out = []
    for g in data.get("groups", {}).values():
        if grouping_name in g.get("groupings", []) and g.get("allow_forward", False):
            t = target(g)
            if t:
                out.append(t)
    return out


def forward_groupings_detailed(data: dict):
    """可转发分组（带 Notion 分组编号），按编号排序。返回 [(name, number, count)]。
    只含有【分组编号】的分组（编号是群里选择用的号）。"""
    result = []
    for gname, ginfo in data.get("groupings", {}).items():
        if not ginfo.get("forward_enabled", True):
            continue
        num = ginfo.get("number")
        if num is None:
            continue
        cnt = len(targets_for_grouping(data, gname))
        if cnt > 0:
            result.append((gname, int(num), cnt))
    result.sort(key=lambda x: x[1])
    return result


def grouping_name_by_number(data: dict, number: int):
    """按 Notion 分组编号找分组名。"""
    for gname, ginfo in data.get("groupings", {}).items():
        if ginfo.get("number") == number:
            return gname
    return None


def all_forward_targets(data: dict):
    """所有【允许转发】的群的寻址字符串（去重）——"所有群聊"用。"""
    seen, out = set(), []
    for g in data.get("groups", {}).values():
        if g.get("allow_forward", False):
            t = target(g)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def get_group(data: dict, name: str):
    return data.get("groups", {}).get(name)


# ---------------------------------------------------------------- 变更

def touch_last_seen(name: str) -> None:
    """更新某群最近可见时间（发现已登记群说话时调用）。"""
    with _LOCK:
        data = load()
        g = data["groups"].get(name)
        if g:
            g["last_seen"] = datetime.now().isoformat(timespec="seconds")
            save(data)


def add_pending(name: str) -> dict:
    """把一个新发现的群登记为 pending（未归类），返回该群条目。"""
    with _LOCK:
        data = load()
        if name in data["groups"]:
            return data["groups"][name]
        g = {
            "name": name,
            "remark": name + DOG,
            "remark_applied": False,
            "notion_page_id": None,
            "allow_forward": False,   # 未归类前默认不参与转发，安全
            "allow_speak": False,
            "welcome_url": "",
            "groupings": [],
            "status": "pending",
            "last_seen": datetime.now().isoformat(timespec="seconds"),
        }
        data["groups"][name] = g
        save(data)
        return g


def mark_remark_applied(name: str, remark: str) -> None:
    """标记某群已打上🐶备注。"""
    with _LOCK:
        data = load()
        g = data["groups"].get(name)
        if g:
            g["remark"] = remark
            g["remark_applied"] = True
            save(data)


def mark_unreachable(name_or_target: str):
    """把一个"转发无结果/找不到"的群在本地标记为不可达（被踢/解散/改名）：
    allow_forward=False + status="unreachable"，后续转发自动跳过它。
    按 群名 / 备注 / 寻址串 三种都匹配。返回被标记的群名或 None。"""
    with _LOCK:
        data = load()
        for name, g in data.get("groups", {}).items():
            if name == name_or_target or g.get("remark") == name_or_target or target(g) == name_or_target:
                g["allow_forward"] = False
                g["status"] = "unreachable"
                save(data)
                return name
    return None


def record_managed(name: str, page_id: str = None) -> None:
    """记录一个群已纳管（打了🐶 + 可选 Notion page_id）。不存在则新建。
    Phase 3 批量迁移用：微信里发现的群，无论库里有没有，都能落地登记。"""
    with _LOCK:
        data = load()
        g = data["groups"].get(name)
        if not g:
            g = {
                "name": name, "remark": name + DOG, "remark_applied": False,
                "notion_page_id": None, "allow_forward": False, "allow_speak": False,
                "welcome_url": "", "groupings": [], "status": "pending",
                "last_seen": datetime.now().isoformat(timespec="seconds"),
            }
            data["groups"][name] = g
        g["remark"] = name + DOG
        g["remark_applied"] = True
        if page_id:
            g["notion_page_id"] = page_id
        save(data)


def upsert_from_notion(groupings: dict, groups: dict, invite_keywords: dict | None = None) -> dict:
    """用 Notion 拉取结果覆盖分组与群的【人管字段】，保留机器人管的字段
    （remark_applied / last_seen / status=pending 的发现态）。返回合并后的登记表。
    invite_keywords 为 None 表示本次未拉取「迎新拉群」表，保留原值。"""
    with _LOCK:
        data = load()
        data["groupings"] = groupings
        merged = data.get("groups", {})
        # page_id → 旧 key。群在 Notion 里改了名（含手滑多打空格）时按 page_id 认人，
        # 把老条目连同【微信里那个真实存在的备注】一起继承过来，再删掉老 key。
        # 不这么做就会另起一条：新条目的 remark 是「新名🐶」，而微信里的备注还是
        # 「老名🐶」，寻址必然落空 —— 群发时报"群不存在"，且新老两条并存重复转发
        # （2026-08-03 踩到：「 NCC的朋友们17群」前导空格幽灵条目）。
        old_by_pid = {g["notion_page_id"]: k for k, g in merged.items()
                      if g.get("notion_page_id")}
        renamed = []
        for name, incoming in groups.items():
            old = merged.get(name)
            if old is None:
                prev_key = old_by_pid.get(incoming.get("notion_page_id"))
                # prev_key 也在本次 Notion 结果里 = 它自己是个有效群，不能当改名删掉
                if prev_key is not None and prev_key not in groups:
                    old = merged[prev_key]
                    renamed.append((prev_key, name))
            old = old or {}
            # 人管字段以 Notion 为准；remark_applied 取「本地已标记 或 Notion 标题带🐶」
            applied = old.get("remark_applied", False) or bool(incoming.get("notion_marked"))
            g = {
                "name": name,
                # 打过备注才继承老 remark（那是微信里真实存在的会话名，改名后靠它寻址）；
                # 没打过就跟着新名走，等 apply_remark 去打
                "remark": (old.get("remark") if applied else "") or (name + DOG),
                "remark_applied": applied,   # 机器人管 + Notion🐶标记兜底
                "notion_page_id": incoming.get("notion_page_id"),
                "allow_forward": incoming.get("allow_forward", False),
                "allow_speak": incoming.get("allow_speak", False),
                "welcome_url": incoming.get("welcome_url", ""),
                "groupings": incoming.get("groupings", []),
                "status": "active",
                "last_seen": old.get("last_seen"),
            }
            merged[name] = g
        # 一个 Notion 行只该对应一条登记。pid 已被本次结果里的某个 key 认领、
        # 自己又不在本次结果里 = 过期重名残留（改名迁移的老 key，或手滑空格留下的
        # 幽灵条目），删掉。幽灵条目最毒的地方是它也带着 allow_forward=True，
        # 会跟真群并存在同一个分组里，每次群发都多转发一次并报"群不存在"。
        incoming_pids = {g.get("notion_page_id") for g in groups.values()
                         if g.get("notion_page_id")}
        for k in [k for k, g in merged.items()
                  if k not in groups and g.get("notion_page_id") in incoming_pids]:
            merged.pop(k, None)
        data["groups"] = merged
        data["renamed_last_sync"] = [{"from": a, "to": b} for a, b in renamed]
        if invite_keywords is not None:
            data["invite_keywords"] = invite_keywords
        data["synced_at"] = datetime.now().isoformat(timespec="seconds")
        save(data)
        return data
