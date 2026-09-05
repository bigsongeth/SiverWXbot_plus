# -*- coding: utf-8 -*-
"""记忆文件上限：超过 max_bytes 的 .md 按行截尾，末尾打标记。递归扫 memory_dir。"""
from __future__ import annotations
import os
from typing import List

MARK = "\n[已截尾]\n"


def enforce(memory_dir: str, max_bytes: int) -> List[str]:
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
