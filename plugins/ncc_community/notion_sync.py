# -*- coding: utf-8 -*-
"""Notion 同步 —— 引擎的①（拉取分组/权限）和④的一半（回写新发现群）。

真相源是 Notion：
- pull(): 读「群聊列表」+「转发群聊分组」→ 写本地 registry.json（人管字段以 Notion 为准）
- push_discovery(): 新发现群 append 到「群聊列表」底部（群名=原群名），让人去归类

token 存 data/secret.json（不进 git）。Notion API 在 win-shukong 可直连（已验证）。
纯标准库实现（urllib），不引第三方依赖。
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

from . import registry
from .common import log

_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_PATH = os.path.join(_DIR, "data", "secret.json")

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"

# 两个数据库（page id），来自 Notion「NCC 社群管理」页
DB_GROUPS = "1564e93f56828007b10cd8a5d2fa1f50"      # 群聊列表
DB_GROUPINGS = "1564e93f568280baa110f5c48b5249b6"   # 转发群聊分组


def _load_secret() -> dict:
    try:
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _token() -> str:
    return (_load_secret().get("notion_token") or "").strip()


def _headers():
    return {
        "Authorization": "Bearer " + _token(),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _api(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- 读工具

def _title(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in (prop or {}).get("title", []))


def _rich(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in (prop or {}).get("rich_text", []))


def _checkbox(prop: dict) -> bool:
    return bool((prop or {}).get("checkbox"))


def _number(prop: dict):
    return (prop or {}).get("number")


def _url(prop: dict) -> str:
    return (prop or {}).get("url") or ""


def _relation_ids(prop: dict):
    return [r["id"] for r in (prop or {}).get("relation", [])]


def _query_all(dbid: str):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        d = _api("POST", f"/databases/{dbid}/query", body)
        rows += d.get("results", [])
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")
    return rows


# ---------------------------------------------------------------- 拉取（Notion → 本地）

def parse_notion(group_rows: list, grouping_rows: list):
    """把 Notion 两个库的原始行解析成 registry 需要的 (groupings, groups)。
    纯函数，便于单测。"""
    # page_id -> 分组名
    gid_to_name = {row["id"]: _title(row["properties"].get("组名")) for row in grouping_rows}

    groupings = {}
    for row in grouping_rows:
        name = _title(row["properties"].get("组名"))
        if not name:
            continue
        groupings[name] = {
            "number": _number(row["properties"].get("分组编号")),
            "forward_enabled": _checkbox(row["properties"].get("是否转发")),
        }

    groups = {}
    for row in group_rows:
        p = row["properties"]
        name = _title(p.get("群名"))
        if not name:
            continue
        grouping_names = [gid_to_name.get(i) for i in _relation_ids(p.get("转发群聊分组"))]
        grouping_names = [g for g in grouping_names if g]
        groups[name] = {
            "notion_page_id": row["id"],
            "allow_forward": _checkbox(p.get("允许转发")),
            "allow_speak": _checkbox(p.get("允许发言")),
            "welcome_url": _url(p.get("迎新推送链接（填写后视为开启）")),
            "groupings": grouping_names,
        }
    return groupings, groups


def pull() -> dict:
    """从 Notion 拉取并写入本地 registry.json。返回统计 dict。"""
    if not _token():
        raise RuntimeError("缺少 Notion token（data/secret.json）")
    group_rows = _query_all(DB_GROUPS)
    grouping_rows = _query_all(DB_GROUPINGS)
    groupings, groups = parse_notion(group_rows, grouping_rows)
    registry.upsert_from_notion(groupings, groups)
    stat = {"groupings": len(groupings), "groups": len(groups),
            "forward_on": sum(1 for g in groups.values() if g["allow_forward"])}
    log("INFO", f"Notion 拉取完成：{stat}")
    return stat


# ---------------------------------------------------------------- 回写（本地 → Notion）

def find_page_by_name(name: str):
    """按群名在「群聊列表」里找已有行，返回 page_id 或 None。"""
    d = _api("POST", f"/databases/{DB_GROUPS}/query",
             {"filter": {"property": "群名", "title": {"equals": name}}, "page_size": 1})
    res = d.get("results", [])
    return res[0]["id"] if res else None


def push_discovery(name: str) -> str:
    """新发现群 append 到「群聊列表」底部（若不存在）。返回 page_id。
    只写群名（title），分组/权限留给人在 Notion 里归类。"""
    if not _token():
        raise RuntimeError("缺少 Notion token")
    existing = find_page_by_name(name)
    if existing:
        return existing
    d = _api("POST", "/pages", {
        "parent": {"database_id": DB_GROUPS},
        "properties": {"群名": {"title": [{"text": {"content": name}}]}},
    })
    log("INFO", f"新群已写入 Notion 待归类：{name}")
    return d["id"]
