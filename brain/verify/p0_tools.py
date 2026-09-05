# -*- coding: utf-8 -*-
"""期 0 验证 ②：指定模型调 MCP 工具的成功率（10 次），以及 patch 是否真把 bash 等工具关掉了。

用法：SONGKEY_API_KEY=... MODEL=songkey-auto python3 brain/verify/p0_tools.py
判据：ok >= 9/10 才能用该模型当主用；tools 列表里不得出现 bash。
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from brain.gateway import profile  # noqa: E402
from brain.gateway.dsh_client import DshClient  # noqa: E402

NODE = os.path.expanduser("~/.nvm/versions/node/v24.19.0/bin/node")
DSH_BIN = os.path.expanduser("~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js")
KEY = os.environ["SONGKEY_API_KEY"]
MODEL = os.environ.get("MODEL", "songkey-auto")
N = int(os.environ.get("N", "10"))

root = tempfile.mkdtemp(prefix="p0tools_")
ws = os.path.join(root, "ws"); os.makedirs(ws)
home = os.path.join(root, "dsh-home")
profile.prepare_dsh_home(home, KEY, MODEL)
patch = os.path.join(root, "patch.yml")
open(patch, "w", encoding="utf-8").write(profile.render_patch(
    "你是测试助手。用户要你调用工具时必须真的调用工具，不要口头假装。",
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo_mcp.py")], None, KEY))
env = dict(os.environ, DSH_HOME=home, SONGKEY_API_KEY=KEY)

# patch 生效检查：dump-config 里 tool-bash 必须 disabled: true
dump = subprocess.run([NODE, DSH_BIN, "--profile", "sdk", "--patch", patch, "--dump-config"],
                      env=env, cwd=ws, capture_output=True, text=True, timeout=120).stdout
i = dump.find("id: tool-bash")
print("PATCH tool-bash block:", dump[i:i + 80].replace("\n", " | "))
print("PATCH persona present:", "你是测试助手" in dump)

c = DshClient(profile.dsh_argv(NODE, DSH_BIN, patch), cwd=ws, env=env, stderr_path=os.path.join(root, "dsh.err"))
c.start(); c.initialize(ws, "songkey", MODEL)
r = c.prompt("list", "列出你现在能用的全部工具名，逗号分隔，不要解释。", 90)
print("TOOLS:", r.text.replace("\n", " ")[:400])
ok = 0
for k in range(N):
    t0 = time.time()
    r = c.prompt(f"t{k}", f"调用 echo 工具，text 参数填 ping{k}，然后把工具返回的原文告诉我。", 90)
    hit = f"ECHO:ping{k}" in r.text or f"ECHO:ping{k}" in str(r.events)
    ok += hit
    print(f"#{k} {'ok ' if hit else 'BAD'} {time.time()-t0:.1f}s text={r.text[:60]!r} timed_out={r.timed_out}")
c.stop()
print(f"RESULT: model={MODEL} tool_calls_ok={ok}/{N}")
