# -*- coding: utf-8 -*-
"""期 0 验证 ①：dsh sdk 重启后，同一个 sessionId 是否接续上文。

用法：SONGKEY_API_KEY=... python3 brain/verify/p0_resume.py
判据：第二个进程里问"我叫什么"，答案含"松爸" → 接续 OK；否则网关必须自己预热（见设计 §4.5）。
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from brain.gateway import profile  # noqa: E402
from brain.gateway.dsh_client import DshClient  # noqa: E402

NODE = os.path.expanduser("~/.nvm/versions/node/v24.19.0/bin/node")
DSH_BIN = os.path.expanduser("~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js")
KEY = os.environ["SONGKEY_API_KEY"]
MODEL = os.environ.get("MODEL", "songkey-auto")

root = tempfile.mkdtemp(prefix="p0resume_")
ws = os.path.join(root, "ws"); os.makedirs(ws)
home = os.path.join(root, "dsh-home")
profile.prepare_dsh_home(home, KEY, MODEL)
patch = os.path.join(root, "patch.yml")
open(patch, "w", encoding="utf-8").write(profile.render_patch(
    "你是测试助手，用一句话中文回答。", [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo_mcp.py")], None, KEY))
env = dict(os.environ, DSH_HOME=home, SONGKEY_API_KEY=KEY)


def boot():
    c = DshClient(profile.dsh_argv(NODE, DSH_BIN, patch), cwd=ws, env=env, stderr_path=os.path.join(root, "dsh.err"))
    c.start(); c.initialize(ws, "songkey", MODEL)
    return c


c = boot()
r1 = c.prompt("resume-test", "记住：我叫松爸。回一句话确认。", 90)
print("turn1:", r1.text, "| timed_out:", r1.timed_out)
c.stop()

c = boot()
r2 = c.prompt("resume-test", "我叫什么？一句话。", 90)
print("turn2 (after restart):", r2.text, "| timed_out:", r2.timed_out)
c.stop()
print("RESULT:", "RESUME_OK" if "松爸" in r2.text else "RESUME_LOST", "| sessions dir:", os.listdir(os.path.join(home, "sessions")))
