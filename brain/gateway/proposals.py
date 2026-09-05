# -*- coding: utf-8 -*-
"""共享知识提议：大脑只能 create，approve/reject 由面板（人）调。approve 才写 shared.md。"""
from __future__ import annotations
import json
import os
import threading
import time
import uuid
from typing import List, Optional


class Proposals:
    def __init__(self, workspace_dir: str):
        self.dir = os.path.join(workspace_dir, "proposals")
        self.shared = os.path.join(workspace_dir, "knowledge", "shared.md")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, pid: str) -> str:
        return os.path.join(self.dir, pid + ".json")

    def _write(self, rec: dict) -> None:
        with open(self._path(rec["id"]), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)

    def _read(self, pid: str) -> dict:
        with open(self._path(pid), encoding="utf-8") as f:
            return json.load(f)

    def create(self, text: str, source: str, conversation: str) -> str:
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self._lock:
            self._write({"id": pid, "text": text.strip(), "source": source.strip(), "conversation": conversation,
                         "status": "pending", "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        return pid

    def list(self, status: Optional[str] = None) -> List[dict]:
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".json"):
                rec = self._read(name[:-5])
                if status is None or rec["status"] == status:
                    out.append(rec)
        return out

    def approve(self, pid: str) -> dict:
        with self._lock:
            rec = self._read(pid)
            if rec["status"] != "approved":
                with open(self.shared, "a", encoding="utf-8") as f:
                    f.write(f"- ({time.strftime('%Y-%m-%d')}，来源: {rec['source']}) {rec['text']}\n")
                rec["status"] = "approved"
                rec["decided"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._write(rec)
            return rec

    def reject(self, pid: str) -> dict:
        with self._lock:
            rec = self._read(pid)
            rec["status"] = "rejected"
            rec["decided"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write(rec)
            return rec
