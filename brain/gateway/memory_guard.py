# -*- coding: utf-8 -*-
"""记忆文件上限：超过 max_bytes 的 .md 按行截尾，末尾打标记。递归扫 memory_dir。

⚠️ 2026-09-08 起网关会并发跑多个会话，多个线程会同时 enforce 同一个 memory 目录 ——
「读全文 → 截断 → 覆写」不是原子的，两个线程交错会把文件写坏（一个读到另一个刚截断到一半的内容）。
模块级一把锁串起来：enforce 是纯本地文件操作、很快，串行不影响吞吐。
"""
from __future__ import annotations
import os
import threading
from typing import List

MARK = "\n[已截尾]\n"

_LOCK = threading.Lock()


def enforce(memory_dir: str, max_bytes: int) -> List[str]:
    with _LOCK:
        return _enforce(memory_dir, max_bytes)


def _enforce(memory_dir: str, max_bytes: int) -> List[str]:
    hit = []
    for root, _dirs, files in os.walk(memory_dir):
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                data = f.read()
            if len(data) <= max_bytes:
                continue
            head = data[:max_bytes].decode("utf-8", errors="ignore")
            head = head[:head.rfind("\n")] if "\n" in head else head
            with open(path, "w", encoding="utf-8") as f:
                f.write(head + MARK)
            hit.append(path)
    return hit
