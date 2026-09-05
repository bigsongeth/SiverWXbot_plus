# -*- coding: utf-8 -*-
"""最近已发气泡：给反口头禅用。全局一份 + 每会话一份，落盘 JSON。"""
from __future__ import annotations
import json
import os
import threading
from collections import deque
from typing import Dict, List


class RecentReplies:
    def __init__(self, path: str, global_n: int, per_conv_n: int):
        self.path, self.gn, self.cn = path, global_n, per_conv_n
        self.g: deque = deque(maxlen=global_n)
        self.c: Dict[str, deque] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.g.extend(d.get("global", []))
        for k, v in (d.get("conversations") or {}).items():
            self.c[k] = deque(v, maxlen=self.cn)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"global": list(self.g), "conversations": {k: list(v) for k, v in self.c.items()}},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, conversation: str, bubbles: List[str]) -> None:
        with self._lock:
            dq = self.c.setdefault(conversation, deque(maxlen=self.cn))
            for b in bubbles:
                dq.append(b)
                self.g.append(b)
            self._save()

    def recent_for(self, conversation: str) -> List[str]:
        with self._lock:
            seen, out = set(), []
            for b in list(self.c.get(conversation, [])) + list(self.g):
                if b not in seen:
                    seen.add(b)
                    out.append(b)
            return out
