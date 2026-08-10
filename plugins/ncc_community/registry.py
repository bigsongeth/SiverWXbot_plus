# -*- coding: utf-8 -*-
"""本地群登记表（registry.json）—— 引擎的地基，也是**唯一真相源**。

2026-08-05 去 Notion 化（PANEL_SPEC.md）：分组/权限/拉群关键词改由 Flask 面板
`/ncc_community` 维护，直接写本模块；Notion 不再读也不再写。
机器人的转发/发现/迎新都读它。

数据结构：
{
  "synced_at": "2026-07-07T01:00:00",   # 遗留字段：最后一次改动时间
  "groupings": {                     # 转发分组
    "大理群": {"number": 4, "forward_enabled": true},
    ...
  },
  "groups": {                        # 键 = 群当前名字
    "NCC大理共居一家人👪": {
      "gid": "3f2a91c7",              # 内部稳定 id，改名时认人用（接替 notion_page_id）
      "name": "NCC大理共居一家人👪",  # 群当前名（寻址用，未打备注前）
      "remark": "NCC大理共居一家人👪🐶", # 目标备注（打上后寻址用这个）
      "remark_applied": false,        # 是否已在微信里打上🐶备注
      "notion_page_id": "…",          # 只读遗留字段，不再使用（回滚保险）
      "allow_forward": true,
      "allow_speak": true,
      "welcome_url": "",
      "groupings": ["大理群"],
      "status": "active",             # active=已归类；pending=新发现未归类；unreachable=转发找不到
      "last_seen": "2026-07-07T…"
    }
  },
  "invite_keywords": {               # 拉群关键词（面板维护，原 Notion「迎新拉群」表）
    "大理": {"group": "NCC大理共居一家人👪", "enabled": true}
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
import uuid
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


def _touch(data: dict) -> None:
    """记一次改动时间（synced_at 是 Notion 时代的名字，去 Notion 后就是"最后改动"）。"""
    data["synced_at"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 内部稳定 id

def new_gid() -> str:
    """新群的内部稳定 id。改名时靠它认人——接替原来 notion_page_id 的职责。"""
    return uuid.uuid4().hex[:8]


def ensure_gids(data: dict) -> int:
    """给还没有 gid 的群补一个（就地改 data，不落盘）。返回补了几个。
    迁移脚本和面板读取时都会调，保证老数据平滑升级。"""
    n = 0
    used = {g.get("gid") for g in data.get("groups", {}).values() if g.get("gid")}
    for g in data.get("groups", {}).values():
        if not g.get("gid"):
            gid = new_gid()
            while gid in used:
                gid = new_gid()
            used.add(gid)
            g["gid"] = gid
            n += 1
    return n


# ---------------------------------------------------------------- 拉群关键词
#
# 面板化之后关键词升级成结构化的 {关键词: {"group": 群名, "enabled": bool}}，
# 这样面板上可以"临时停用某个关键词"而不用删掉重建。
# 老格式 {关键词: 群名} 仍然读得动（一期迁移脚本会就地升级，读侧兜底防漏）。

def invite_entry(value) -> dict:
    """把一条拉群关键词（新旧两种格式）归一成 {"group","enabled"}。"""
    if isinstance(value, dict):
        return {"group": str(value.get("group") or "").strip(),
                "enabled": bool(value.get("enabled", True))}
    return {"group": str(value or "").strip(), "enabled": True}


def invite_map(data: dict) -> dict:
    """真正生效的 {关键词: 目标群}——停用的和目标群为空的都不出现。"""
    out = {}
    for kw, v in (data.get("invite_keywords") or {}).items():
        e = invite_entry(v)
        if e["enabled"] and e["group"]:
            out[str(kw)] = e["group"]
    return out


def invite_items(data: dict) -> list:
    """面板展示用：[(关键词, {"group","enabled"})]，按关键词排序。"""
    return sorted(((str(k), invite_entry(v))
                   for k, v in (data.get("invite_keywords") or {}).items()),
                  key=lambda x: x[0])


# ---------------------------------------------------------------- 寻址

def target(group: dict) -> str:
    """一个群的寻址字符串：打了🐶备注用备注，否则用当前群名。"""
    if group.get("remark_applied") and group.get("remark"):
        return group["remark"]
    return group.get("name", "")


def address_candidates(group: dict) -> list:
    """转发寻址的候选串，按【优先级】排序，转发时依次试，命中即用。

    为什么不能只给一个串（2026-08-04 事故）：
    `remark_applied` 并不代表微信里真打上了备注 —— upsert_from_notion 里它可以由
    「Notion 群名标题末尾带🐶」推断出来，而 8/4 实测改群备注的三条路全部封死、
    备注根本打不上。结果 105 个群的寻址串全是「群名🐶」这种微信里不存在的名字，
    转发的"发送给"对话框搜出来一律无结果，第一个群就把整条线卡死。
    （而「检查群组」用的是宽容得多的主窗口搜索，105/105 全报可达，假绿。）

    ★ 顺序按【微信里的显示名】猜，而不是按"哪个名字更真"（2026-08-10 实测修正）：
    "发送给"对话框里，wxautox 要在搜索结果里找**显示名与目标串一致**的项才勾得中。
    群一旦打了🐶备注，微信显示的就是备注本身，这时拿群名去搜——**搜得到，但勾不中**：
    结果列出来了，单选框没勾上，"发送"是灰的，send() 在里面死等。
    8/4 我反过来把群名排在了前面（当时以为备注根本没打上），结果每个已打🐶的群都要
    先白等一次超时才换备注，105 个群平白多耗 20 分钟。实测日志（8/10 23:45）
    「昆山NCC林克岛的朋友们2」正是靠备选串「…2🐶」命中的。

    所以：标着已打备注的群，备注优先；没打过的，群名优先。两个都留着依次试——
    remark_applied 未必可信（它还兼着"Notion 标题带🐶=已纳管"的语义），
    猜错顶多多花一次超时，猜不到才会真的发不出去。
    上次实测命中过的串（addressing_hit）永远排最前，第二轮起就不用再猜。"""
    name = str(group.get("name") or "").strip()
    remark = str(group.get("remark") or "").strip()
    hit = str(group.get("addressing_hit") or "").strip()
    order = (hit, remark, name) if group.get("remark_applied") else (hit, name, remark)
    out = []
    for c in order:
        if c and c not in out:
            out.append(c)
    return out


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


def forward_specs(data: dict, grouping_name: str = None):
    """转发用的目标清单：每个群一条 {"name": 群名, "cands": [寻址候选…]}。

    与 targets_for_grouping/all_forward_targets 的区别是【带候选】：单一寻址串
    一旦不对（比如备注其实没打上），转发就会在"发送给"对话框里搜不到而卡死，
    详见 address_candidates 的注释。分组名传 None 表示"所有群聊"。"""
    out, seen = [], set()
    for name, g in data.get("groups", {}).items():
        if not g.get("allow_forward", False):
            continue
        if grouping_name is not None and grouping_name not in g.get("groupings", []):
            continue
        cands = address_candidates(g)
        if not cands or cands[0] in seen:
            continue
        seen.add(cands[0])
        out.append({"name": name, "cands": cands})
    return out


def mark_addressing(name: str, hit: str) -> None:
    """记下某群实际是靠哪个串搜到的，下次寻址从它开始试。

    只写 addressing_hit 这个独立字段，【不动 remark_applied】——后者兼着
    "Notion 标题带🐶=已纳管"的语义，是备注工作流的地盘，改它会打架。"""
    if not name or not hit:
        return
    with _LOCK:
        data = load()
        g = data.get("groups", {}).get(name)
        if not g or g.get("addressing_hit") == hit:
            return
        g["addressing_hit"] = hit
        save(data)


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
            "gid": new_gid(),
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
                "gid": new_gid(),
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


# ---------------------------------------------------------------- 面板 CRUD
#
# 去 Notion 化后，"人维护的字段"全部由面板 /ncc_community 经这些函数落盘。
# 约定：
#   - 全部在 _LOCK 内 load→改→save，面板与 bot 同进程共享这把锁，无跨进程竞争；
#   - 参数不合法一律 raise ValueError（中文），由面板层转成错误提示，不静默吞；
#   - 群名是寻址主键，一切入口都 strip()，禁止空名——Notion 时代一个前导空格
#     就造出过「幽灵群」（CLAUDE.md 3.6）。

_EDITABLE_FIELDS = ("allow_forward", "allow_speak", "welcome_url", "groupings", "remark")


def _clean_name(name: str) -> str:
    n = str(name or "").strip()
    if not n:
        raise ValueError("群名不能为空")
    return n


def _check_groupings(data: dict, names) -> list:
    """校验分组名都存在——拼错一个字就会造出一个谁也发不到的死分组。"""
    out = []
    for x in (names or []):
        s = str(x).strip()
        if not s:
            continue
        if s not in data.get("groupings", {}):
            raise ValueError(f"没有「{s}」这个分组")
        if s not in out:
            out.append(s)
    return out


def add_group(name: str, **fields) -> dict:
    """手工新增一个群（Notion 时代是在「群聊列表」加一行）。

    正常路径是群里有人说话时 discovery 自动登记；这个入口是给"群还没说过话，
    但我现在就想把它配好"用的。默认 active、不参与转发，配置由调用方补。"""
    name = _clean_name(name)
    with _LOCK:
        data = load()
        if name in data["groups"]:
            raise ValueError(f"「{name}」已经在登记表里了")
        g = {
            "gid": new_gid(),
            "name": name,
            "remark": name + DOG,
            "remark_applied": False,
            "notion_page_id": None,
            "allow_forward": False,
            "allow_speak": False,
            "welcome_url": "",
            "groupings": [],
            "status": "active",
            "last_seen": None,
        }
        if fields:
            _apply_fields(data, g, fields)
        data["groups"][name] = g
        _touch(data)
        save(data)
        return g


def delete_group(name: str) -> None:
    """从登记表里删掉一个群（退群/解散后清理）。连带清掉指向它的拉群关键词，
    否则关键词会指向一个不存在的群，用户一发就是必然失败。"""
    name = _clean_name(name)
    with _LOCK:
        data = load()
        if name not in data["groups"]:
            raise ValueError(f"登记表里没有「{name}」")
        data["groups"].pop(name)
        for kw, v in list((data.get("invite_keywords") or {}).items()):
            if invite_entry(v)["group"] == name:
                data["invite_keywords"].pop(kw, None)
        _touch(data)
        save(data)


def _apply_fields(data: dict, g: dict, fields: dict) -> None:
    """把面板传来的字段写进群条目（就地改，调用方负责保存）。"""
    for k, v in fields.items():
        if k not in _EDITABLE_FIELDS:
            raise ValueError(f"不可编辑的字段：{k}")
        if k == "groupings":
            g["groupings"] = _check_groupings(data, v)
        elif k in ("allow_forward", "allow_speak"):
            g[k] = bool(v)
        elif k == "remark":
            g["remark"] = str(v or "").strip()
        else:
            g[k] = str(v or "").strip()


def set_group_fields(name: str, **fields) -> dict:
    """改一个群的人管字段（分组/允许转发/允许发言/迎新链接）。"""
    name = _clean_name(name)
    with _LOCK:
        data = load()
        g = data["groups"].get(name)
        if not g:
            raise ValueError(f"登记表里没有「{name}」")
        _apply_fields(data, g, fields)
        _touch(data)
        save(data)
        return g


def rename_group(old: str, new: str) -> dict:
    """群在微信里改了名，在面板上同步过来：换 key、保 gid、继承备注。

    Notion 时代这一步是隐式的（同步时按 page_id 认人自动迁移），现在改成人
    显式触发——隐式批量迁移出过事（幽灵群、同步悄悄复活坏群），显式更好排查。

    继承规则复刻 upsert_from_notion 那套：
    - **打过备注的群继承老 remark**：那是微信里真实存在的会话名，群名改了也照样
      能靠它寻址；不继承的话 remark 变成「新名🐶」而微信里还是「老名🐶」，必然落空。
    - 没打过备注的跟新名走（remark = 新名🐶），等打备注时再打。
    - addressing_hit 若等于老群名则丢弃（那个串已经不存在了），等于备注则留着。
    - 指向老名的拉群关键词一并改到新名，否则关键词当场失效。"""
    old, new = _clean_name(old), _clean_name(new)
    with _LOCK:
        data = load()
        g = data["groups"].get(old)
        if not g:
            raise ValueError(f"登记表里没有「{old}」")
        if new == old:
            return g
        if new in data["groups"]:
            raise ValueError(f"「{new}」已经在登记表里了，不能改成重名")
        g["name"] = new
        if not g.get("remark_applied"):
            g["remark"] = new + DOG
        if str(g.get("addressing_hit") or "") == old:
            g["addressing_hit"] = None
        data["groups"].pop(old)
        data["groups"][new] = g
        for kw, v in list((data.get("invite_keywords") or {}).items()):
            e = invite_entry(v)
            if e["group"] == old:
                e["group"] = new
                data["invite_keywords"][kw] = e
        _touch(data)
        save(data)
        return g


def classify_pending(name: str, groupings=None, allow_forward: bool = False,
                     allow_speak: bool = False, welcome_url: str = "") -> dict:
    """把一个 discovery 发现的新群归类（Notion 时代是去表里选分组+勾权限）。
    归类后 status 从 pending 变 active，它才会进入转发目标。"""
    name = _clean_name(name)
    with _LOCK:
        data = load()
        g = data["groups"].get(name)
        if not g:
            raise ValueError(f"登记表里没有「{name}」")
        g["groupings"] = _check_groupings(data, groupings)
        g["allow_forward"] = bool(allow_forward)
        g["allow_speak"] = bool(allow_speak)
        g["welcome_url"] = str(welcome_url or "").strip()
        g["status"] = "active"
        g.setdefault("gid", new_gid())
        _touch(data)
        save(data)
        return g


def restore_reachable(name: str) -> dict:
    """人工把一个被标记 unreachable 的群恢复回来（确认它还在、名字也对了之后）。

    Notion 时代"复活"是靠改 Notion + 同步，而同步是【无条件覆盖】的：坏群会在
    人还没核实的情况下被悄悄恢复成 allow_forward=True，下一轮群发照样卡在它身上。
    现在必须人在面板上点一下，恢复这件事有据可查。"""
    name = _clean_name(name)
    with _LOCK:
        data = load()
        g = data["groups"].get(name)
        if not g:
            raise ValueError(f"登记表里没有「{name}」")
        g["status"] = "active"
        g["allow_forward"] = True
        g["addressing_hit"] = None      # 上次命中的串多半已失效，重新按候选试
        _touch(data)
        save(data)
        return g


def set_grouping(name: str, number=None, forward_enabled: bool = True) -> dict:
    """新增/修改一个转发分组。number 是群里选择用的编号，必须唯一——
    重号时 grouping_name_by_number 只会认到第一个，另一个永远选不中。"""
    name = _clean_name(name)
    if number in ("", None):
        number = None
    else:
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise ValueError(f"分组编号必须是数字：{number!r}")
        if number == 1:
            raise ValueError("编号 1 被「所有群聊」占用了，换一个")
    with _LOCK:
        data = load()
        if number is not None:
            for other, info in data.get("groupings", {}).items():
                if other != name and info.get("number") == number:
                    raise ValueError(f"编号 {number} 已经被分组「{other}」占用")
        info = data.setdefault("groupings", {}).setdefault(name, {})
        info["number"] = number
        info["forward_enabled"] = bool(forward_enabled)
        _touch(data)
        save(data)
        return info


def rename_grouping(old: str, new: str) -> dict:
    """改分组名，并把所有群身上的引用一起改掉（不然那些群会挂在一个死分组上）。"""
    old, new = _clean_name(old), _clean_name(new)
    with _LOCK:
        data = load()
        if old not in data.get("groupings", {}):
            raise ValueError(f"没有「{old}」这个分组")
        if new == old:
            return data["groupings"][old]
        if new in data["groupings"]:
            raise ValueError(f"「{new}」这个分组已经存在")
        data["groupings"][new] = data["groupings"].pop(old)
        for g in data.get("groups", {}).values():
            gs = g.get("groupings") or []
            if old in gs:
                g["groupings"] = [new if x == old else x for x in gs]
        _touch(data)
        save(data)
        return data["groupings"][new]


def delete_grouping(name: str) -> None:
    """删一个分组，并从所有群身上摘掉它。"""
    name = _clean_name(name)
    with _LOCK:
        data = load()
        if name not in data.get("groupings", {}):
            raise ValueError(f"没有「{name}」这个分组")
        data["groupings"].pop(name)
        for g in data.get("groups", {}).values():
            gs = g.get("groupings") or []
            if name in gs:
                g["groupings"] = [x for x in gs if x != name]
        _touch(data)
        save(data)


def set_invite_keyword(keyword: str, group: str, enabled: bool = True) -> dict:
    """新增/修改一条拉群关键词。目标群必须在登记表里——拼错群名的关键词
    是个哑弹：用户发了、机器人找不到群，只会失败退配额。"""
    kw = str(keyword or "").strip()
    if not kw:
        raise ValueError("关键词不能为空")
    group = _clean_name(group)
    with _LOCK:
        data = load()
        if group not in data.get("groups", {}):
            raise ValueError(f"登记表里没有「{group}」这个群")
        entry = {"group": group, "enabled": bool(enabled)}
        data.setdefault("invite_keywords", {})[kw] = entry
        _touch(data)
        save(data)
        return entry


def delete_invite_keyword(keyword: str) -> None:
    kw = str(keyword or "").strip()
    with _LOCK:
        data = load()
        if kw not in (data.get("invite_keywords") or {}):
            raise ValueError(f"没有「{kw}」这个拉群关键词")
        data["invite_keywords"].pop(kw)
        _touch(data)
        save(data)


# ---------------------------------------------------------------- 遗留：Notion 同步
#
# 2026-08-05 起【无任何调用点】（去 Notion 化，见 PANEL_SPEC.md）。
# 保留代码是为了 git revert 能一步回滚，别在新代码里再挂上它。

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
            # 人管字段以 Notion 为准；remark_applied **只认本地实打实标记过的**。
            # 原来这里还认「Notion 标题带🐶」当兜底，是 8/4 事故的假绿来源之一：
            # 人在 Notion 里手敲一个🐶，登记表就以为微信里已有备注，寻址串用
            # 「群名🐶」而微信里根本没这备注（CLAUDE.md 3.6）。本地记录丢了就跑
            # 「修备注 全部」从微信侧重建，别再让 Notion 替微信作证。
            applied = old.get("remark_applied", False)
            g = {
                "gid": old.get("gid") or new_gid(),
                "name": name,
                # 打过备注才继承老 remark（那是微信里真实存在的会话名，改名后靠它寻址）；
                # 没打过就跟着新名走，等 apply_remark 去打
                "remark": (old.get("remark") if applied else "") or (name + DOG),
                "remark_applied": applied,   # 机器人管 + Notion🐶标记兜底
                # 实测命中的寻址串（机器人管，Notion 同步不该抹掉它）——见 address_candidates
                "addressing_hit": old.get("addressing_hit"),
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
