# -*- coding: utf-8 -*-
"""打🐶成功之后的统一收尾（2026-09-13，群🐶自动打标方案 A，提案 §7.2）。

不管备注是谁打的（全局监听当场打 / 「修备注 <群名>」后台任务打），打完都走这里：
  1. 登记表：add_pending（幂等）+ mark_remark_applied —— 寻址串从此切到「群名🐶」；
  2. 若这个群在 `config.group` 里【独立监听】：同步机器人的 config/config.json
     —— `group` 列表旧名换新名；`group_api_map` / `group_prompt_map` 旧键保留、新键加上（值相同）；
     先备份成 config.json.bak-<日期>。为什么监听中的群必须同步：wxautox 找会话是拿【显示名】
     精确匹配，打完备注显示名就是「群名🐶」，下次重启按旧名 AddListenChat 找不到 → 群整个聋掉
     （CLAUDE.md 3.6「2026-09-06 美食群断了 1 小时」）。
     ★ 内存里的 bot.config.group 是【追加新名、保留旧名】而不是替换：运行中的子窗口 chat.who
     可能仍是旧名（也可能已刷成新名，没实测过），两个都认才不会把这个群的消息判成"不在监听范围"。
     两个 map 旧键保留同理。文件里 group 列表则是替换（重启时只按新名开监听，不白搜一次旧名）。
  3. 复制 memory/<wxid>/<旧名>/ 成 <新名>/（保历史，不删旧目录）；
  4. 飞书通知（webhook_send.send_message），带两句人必须做的提醒：
     面板页面先点「加载配置」再点保存；brain/workspace/skills/index.json 两个名字都列上。
     一律不发管理群（用户 2026-09-13 拍板 Q3=b）。

任何一步失败都只记进 errors 并写进通知，绝不抛出 —— 备注已经打上了，收尾失败也得让人知道。
刻意不 import wxbot_core（会连带拉起 wxautox），只鸭子类型地用 bot.config / bot.memory_manager。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

from . import registry
from .common import log

REMINDER_PANEL = ("⚠️ 面板页面先点「加载配置」再点「保存配置」/「重启载入新配置」——"
                  "没刷新的旧页面会把改前的 config.json 整份写回去（2026-09-06 被盖回去 6 次）。")
REMINDER_BRAIN = ("⚠️ brain/workspace/skills/index.json 是按群名精确匹配的：旧名、新名两个名字都列上，"
                  "再 deploy.sh --remote。")

_SOURCE_LABEL = {
    "global": "全局监听读到该群首条消息，当场打的",
    "listened": "「修备注 <群名>」（监听中的群，bot 后台任务）",
    "manual": "管理指令",
}


def notify(title: str, content: str) -> bool:
    """发飞书 webhook。失败/异常只记日志，返回 False。"""
    try:
        import webhook_send
        ok, msg = webhook_send.send_message(title, content)
        if not ok:
            log("WARNING", f"飞书通知失败「{title}」：{msg}")
        return bool(ok)
    except Exception as e:
        log("WARNING", f"飞书通知异常「{title}」：{e}")
        return False


def _panel_url() -> str:
    try:
        from . import panel
        return panel.panel_url()
    except Exception:
        return ""


def _runtime_group_list(bot):
    try:
        lst = getattr(getattr(bot, "config", None), "group", None)
        return list(lst) if isinstance(lst, (list, tuple, set)) else []
    except Exception:
        return []


def is_listened(bot, name: str) -> bool:
    """这个群在机器人的独立监听列表（config.group）里吗。"""
    return name in _runtime_group_list(bot)


# ---------------------------------------------------------------- config.json 同步

def sync_bot_config(bot, old_name: str, new_name: str) -> dict:
    """把 config.json 三处键从旧名同步到新名（文件 + 内存），先备份。
    返回 {"synced": bool, "backup": path|None, "changed": [...], "error": str}。"""
    res = {"synced": False, "backup": None, "changed": [], "error": ""}
    cfg_obj = getattr(bot, "config", None)
    path = getattr(cfg_obj, "CONFIG_FILE", None)
    if not path:
        res["error"] = "机器人配置对象没有 CONFIG_FILE，找不到 config.json"
        return res
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        res["error"] = f"读 config.json 失败：{e}"
        return res
    if not isinstance(data, dict):
        res["error"] = "config.json 顶层不是对象"
        return res

    changed = []
    groups = data.get("group")
    if isinstance(groups, list) and old_name in groups:
        if new_name in groups:
            groups.remove(old_name)
        else:
            groups[groups.index(old_name)] = new_name
        changed.append("group")
    for key in ("group_api_map", "group_prompt_map"):
        m = data.get(key)
        if isinstance(m, dict) and old_name in m and new_name not in m:
            m[new_name] = m[old_name]
            changed.append(key)

    if changed:
        # 备份：同一天第一份最值钱（改前状态），已存在就不覆盖、换带时分秒的名字
        bak = f"{path}.bak-{datetime.now():%Y%m%d}"
        if os.path.exists(bak):
            bak = f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            shutil.copyfile(path, bak)
            res["backup"] = bak
        except Exception as e:
            res["error"] = f"备份 config.json 失败：{e}"
            return res
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp, path)
        except Exception as e:
            res["error"] = f"写 config.json 失败：{e}"
            try:
                os.remove(tmp)
            except OSError:
                pass
            return res
    res["changed"] = changed
    res["synced"] = True
    _sync_runtime_config(cfg_obj, old_name, new_name)
    return res


def _sync_runtime_config(cfg_obj, old_name: str, new_name: str) -> None:
    """内存里的配置：group 追加新名（保留旧名）、两个 map 加新键。
    wxbot_core 里 self.group / self.group_api_map 与 self.config['group'] 等是同一个对象，
    但也可能不是（refresh_config 之后），所以两边都看、按 id 去重。"""
    if cfg_obj is None:
        return
    cfg_dict = getattr(cfg_obj, "config", None)
    if not isinstance(cfg_dict, dict):
        cfg_dict = {}
    seen = set()
    for lst in (getattr(cfg_obj, "group", None), cfg_dict.get("group")):
        if isinstance(lst, list) and id(lst) not in seen:
            seen.add(id(lst))
            if new_name not in lst:
                lst.append(new_name)
    for key in ("group_api_map", "group_prompt_map"):
        for m in (getattr(cfg_obj, key, None), cfg_dict.get(key)):
            if isinstance(m, dict) and id(m) not in seen:
                seen.add(id(m))
                if old_name in m and new_name not in m:
                    m[new_name] = m[old_name]


# ---------------------------------------------------------------- 记忆目录

def copy_memory_dir(bot, old_name: str, new_name: str) -> tuple[bool, str]:
    """memory/<wxid>/<旧名>/ 复制成 <新名>/，里面的 <旧名>_memory.json 改名成 <新名>_memory.json。
    目录名走 MemoryManager._resolve_storage_name（非法字符剔除 / hash 兜底），没有就用原名。"""
    mm = getattr(bot, "memory_manager", None)
    base = getattr(mm, "base_path", None)
    wx_id = getattr(mm, "wx_id", None)
    if not isinstance(base, str) or not isinstance(wx_id, str) or not base or not wx_id:
        return False, "没有记忆管理器，跳过"
    resolve = getattr(mm, "_resolve_storage_name", None)

    def _storage(n):
        try:
            r = resolve(n) if callable(resolve) else n
        except Exception:
            r = n
        return str(r[0] if isinstance(r, tuple) else r)

    old_st, new_st = _storage(old_name), _storage(new_name)
    src = os.path.join(base, wx_id, old_st)
    dst = os.path.join(base, wx_id, new_st)
    if not os.path.isdir(src):
        return False, f"旧记忆目录不存在（{src}），没有历史可复制"
    if os.path.exists(dst):
        return False, f"新记忆目录已存在（{dst}），不覆盖"
    shutil.copytree(src, dst)
    old_file = os.path.join(dst, f"{old_st}_memory.json")
    new_file = os.path.join(dst, f"{new_st}_memory.json")
    if os.path.exists(old_file) and not os.path.exists(new_file):
        os.replace(old_file, new_file)
    if new_st != new_name:
        try:
            with open(os.path.join(dst, "name.json"), "w", encoding="utf-8") as f:
                json.dump({"name": str(new_name)}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return True, dst


# ---------------------------------------------------------------- 统一收尾

def post_tag(bot, old_name: str, new_name: str, source: str = "global") -> dict:
    """打标成功后的收尾。old_name = 打之前的显示名（真实群名），new_name = 打上的备注。
    返回 {"config_synced","memory_copied","backup","errors",...}，绝不抛出。"""
    old = str(old_name or "").strip()
    new = str(new_name or "").strip()
    res = {"config_synced": False, "memory_copied": False, "backup": None,
           "config_changed": [], "memory_info": "", "errors": [], "listened": False}

    try:
        registry.add_pending(old)
        registry.mark_remark_applied(old, new)
    except Exception as e:
        res["errors"].append(f"登记表写入失败：{e}")
        log("ERROR", f"打标收尾：登记表写入失败 {old}: {e}")

    listened = is_listened(bot, old)
    res["listened"] = listened
    if listened:
        try:
            r = sync_bot_config(bot, old, new)
            res["config_synced"] = bool(r.get("synced"))
            res["backup"] = r.get("backup")
            res["config_changed"] = list(r.get("changed") or [])
            if r.get("error"):
                res["errors"].append(f"同步 config.json 失败：{r['error']}")
        except Exception as e:
            res["errors"].append(f"同步 config.json 失败：{e}")
            log("ERROR", f"打标收尾：同步 config.json 失败 {old}: {e}")
        try:
            ok, info = copy_memory_dir(bot, old, new)
            res["memory_copied"], res["memory_info"] = ok, info
        except Exception as e:
            res["errors"].append(f"复制记忆目录失败：{e}")
            log("ERROR", f"打标收尾：复制记忆目录失败 {old}: {e}")

    notify(f"群🐶已打标：{old}", _build_notice(old, new, source, res))
    log("INFO", f"打标收尾完成 {old} -> {new}（监听中={listened} config同步={res['config_synced']} "
                f"记忆复制={res['memory_copied']} 错误={len(res['errors'])}）")
    return res


def _build_notice(old: str, new: str, source: str, res: dict) -> str:
    lines = [f"群「{old}」已打上🐶备注 → 「{new}」",
             f"来源：{_SOURCE_LABEL.get(source, source)}",
             f"登记表：已记 remark_applied，寻址串切到「{new}」。"]
    url = _panel_url()
    if url:
        lines.append(f"去面板归类（分组 / 允许转发）：{url}")
    if res.get("listened"):
        lines.append("")
        lines.append("这个群在 config.group 里独立监听，已同步 config/config.json：")
        if res.get("config_synced"):
            ch = "、".join(res.get("config_changed") or []) or "（三处键里没有旧名，没改）"
            lines.append(f"  · 改了：{ch}（group 列表旧名换新名；两个 map 旧键保留、新键加上）")
            if res.get("backup"):
                lines.append(f"  · 备份：{res['backup']}")
        else:
            lines.append("  · ❌ 没同步成功，见下面错误 —— 请人工改 group / group_api_map / group_prompt_map")
        if res.get("memory_copied"):
            lines.append(f"  · 记忆目录已复制到：{res.get('memory_info')}")
        else:
            lines.append(f"  · 记忆目录未复制：{res.get('memory_info') or '未知原因'}")
        lines.append("  · 运行中的进程两个名字都认，不用立刻重启；下次重启按新名开监听。")
    else:
        lines.append("这个群不在独立监听列表里，config.json 没动。若以后要加进监听，用新名「%s」。" % new)
    lines.append("")
    lines.append(REMINDER_PANEL)
    lines.append(REMINDER_BRAIN)
    if res.get("errors"):
        lines.append("")
        lines.append("❌ 收尾时的错误：")
        lines.extend(f"  - {e}" for e in res["errors"])
    return "\n".join(lines)
