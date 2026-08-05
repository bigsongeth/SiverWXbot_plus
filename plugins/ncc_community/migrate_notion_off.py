# -*- coding: utf-8 -*-
"""一次性迁移：去 Notion 化（PANEL_SPEC.md §5）。

干三件事，**幂等，可以反复跑**：
  1. 备份 registry.json → registry.json.bak-<日期>（同名已存在就不重复备份）
  2. 给每个群补 `gid`（内部稳定 id，接替 notion_page_id 的"改名认人"职责）
  3. 把 config.json 里「设拉群」攒下的本地关键词（invite.keywords）合并进
     registry.invite_keywords，并把关键词升级成结构化 {group, enabled}

关于合并方向：`invite.keywords` 一直是**同名优先**的本地覆盖层（invite.py 里
`keywords.update(icfg[...])`），所以合并时同名以它为准，跟原来的运行时行为一致。
合并完 config.json 里那份**保留不删**——一期先并行放着，回滚时它还是覆盖层。

⚠️ PANEL_SPEC §2 把这里写成了「合并 remark_overrides」，那是笔误：
`remark_overrides` 是「设备注」用的备注字符串覆盖（给超长群名指定短备注用的），
跟拉群关键词是两码事，不能并进来。这里按实际语义合并 `invite.keywords`。

用法（在项目根目录）：
    python3 -m plugins.ncc_community.migrate_notion_off          # 真跑
    python3 -m plugins.ncc_community.migrate_notion_off --dry    # 只看不改
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date

from . import registry, store


def backup_registry() -> str:
    """备份 registry.json，返回备份路径（文件不存在或当天已备份则返回空串）。"""
    src = registry.REGISTRY_PATH
    if not os.path.exists(src):
        return ""
    dst = f"{src}.bak-{date.today().isoformat()}"
    if os.path.exists(dst):
        return ""
    shutil.copy2(src, dst)
    return dst


def merge_invite_keywords(data: dict, local: dict) -> tuple[int, int]:
    """把关键词统一成结构化，并合并本地覆盖层。返回 (升级条数, 合并条数)。"""
    kws = data.setdefault("invite_keywords", {})
    upgraded = 0
    for kw, v in list(kws.items()):
        if not isinstance(v, dict):
            kws[kw] = registry.invite_entry(v)
            upgraded += 1
    merged = 0
    for kw, group in (local or {}).items():
        kw = str(kw).strip()
        group = str(group or "").strip()
        if not kw or not group:
            continue
        entry = registry.invite_entry(kws.get(kw))
        if entry.get("group") == group and kw in kws:
            continue
        kws[kw] = {"group": group, "enabled": True}
        merged += 1
    return upgraded, merged


def run(dry: bool = False) -> dict:
    """执行迁移，返回统计 dict。"""
    stat = {"backup": "", "gids": 0, "upgraded": 0, "merged": 0,
            "groups": 0, "keywords": 0, "dangling": []}
    if not dry:
        stat["backup"] = backup_registry()

    with registry._LOCK:
        data = registry.load()
        stat["gids"] = registry.ensure_gids(data)
        local = ((store.load().get("invite") or {}).get("keywords") or {})
        stat["upgraded"], stat["merged"] = merge_invite_keywords(data, local)
        stat["groups"] = len(data.get("groups", {}))
        stat["keywords"] = len(data.get("invite_keywords", {}))
        # 指向不存在的群的关键词：不自动删（可能只是群名待同步），列出来让人在面板上处理
        known = set(data.get("groups", {}))
        stat["dangling"] = sorted(kw for kw, v in data["invite_keywords"].items()
                                  if registry.invite_entry(v)["group"] not in known)
        if not dry:
            registry._touch(data)
            registry.save(data)
    return stat


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry" in argv or "-n" in argv
    stat = run(dry=dry)
    print(("[预览] " if dry else "") + "去 Notion 化迁移完成：")
    if stat["backup"]:
        print(f"  备份：{stat['backup']}")
    print(f"  补 gid：{stat['gids']} 个（共 {stat['groups']} 群）")
    print(f"  关键词升级结构化：{stat['upgraded']} 条；从 config.json 合并：{stat['merged']} 条"
          f"（共 {stat['keywords']} 条）")
    if stat["dangling"]:
        print(f"  ⚠️ {len(stat['dangling'])} 条关键词指向登记表里没有的群，去面板核对：")
        for kw in stat["dangling"]:
            print(f"     - {kw}")
    print("  详情：" + json.dumps({k: v for k, v in stat.items() if k != "dangling"},
                                ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
