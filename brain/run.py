# -*- coding: utf-8 -*-
"""肥肉大脑入口：准备数据目录 → 渲染 patch → 起网关（网关按需拉起 dsh）。

环境变量：
  FEIROU_DATA      运行数据目录，默认 ~/feirou-brain-data（不进库）
  SONGKEY_API_KEY  必填
  NODE / DSH_BIN   可选，默认 nvm 的 node 24 与全局 dsh
用法：cd 仓库根 && SONGKEY_API_KEY=... python3 brain/run.py
"""
from __future__ import annotations
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from brain.gateway import config, profile  # noqa: E402
from brain.gateway.dsh_client import DshClient  # noqa: E402
from brain.gateway.server import Gateway, serve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(HERE, "workspace")


def seed_workspace(ws: str) -> None:
    """种子只在目标缺失时复制：PERSONA.md / skills / knowledge 每次覆盖（进库的是真相源），
    memory / proposals 只建目录。"""
    for sub in ("memory/people", "memory/groups", "proposals", "knowledge", "skills"):
        os.makedirs(os.path.join(ws, sub), exist_ok=True)
    shutil.copy(os.path.join(SEED, "PERSONA.md"), os.path.join(ws, "PERSONA.md"))
    for name in os.listdir(os.path.join(SEED, "skills")):
        src = os.path.join(SEED, "skills", name)
        dst = os.path.join(ws, "skills", name)
        if os.path.isdir(src):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
    shared = os.path.join(ws, "knowledge", "shared.md")
    if not os.path.exists(shared):
        shutil.copy(os.path.join(SEED, "knowledge", "shared.md"), shared)


def check_key(key: str) -> None:
    """启动时验一次 key：09-05 hzfood 网关静默吃过 401，大脑不能带着废 key 起来。"""
    import urllib.request
    req = urllib.request.Request("https://key.bigsong.site/v1/models", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                sys.exit(f"songkey 返回 {r.status}")
    except Exception as e:
        sys.exit(f"songkey key 校验失败：{e}")


def main() -> None:
    key = os.environ.get("SONGKEY_API_KEY", "")
    if not key:
        sys.exit("SONGKEY_API_KEY 未设置")
    check_key(key)
    data = os.path.expanduser(os.environ.get("FEIROU_DATA", "~/feirou-brain-data"))
    ws = os.path.join(data, "workspace")
    dsh_home = os.path.join(data, "dsh-home")
    node = os.path.expanduser(os.environ.get("NODE", "~/.nvm/versions/node/v24.19.0/bin/node"))
    dsh_bin = os.path.expanduser(os.environ.get(
        "DSH_BIN", "~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"))
    cfg = config.load(data)
    seed_workspace(ws)
    profile.prepare_dsh_home(dsh_home, key, cfg["model"])
    persona = open(os.path.join(ws, "PERSONA.md"), encoding="utf-8").read()
    patch_path = os.path.join(data, "cordis.patch.yml")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(profile.render_patch(persona, [sys.executable, os.path.join(HERE, "mcp", "feirou_tools.py")],
                                     os.path.join(HERE, "mcp", "grok_search", "server.js"), key, node=node))
    env = dict(os.environ, DSH_HOME=dsh_home, SONGKEY_API_KEY=key,
               FEIROU_GW=f"http://127.0.0.1:{cfg['port']}")

    def factory():
        return DshClient(profile.dsh_argv(node, dsh_bin, patch_path), cwd=ws, env=env,
                         stderr_path=os.path.join(data, "log", "dsh.err"))

    os.makedirs(os.path.join(data, "log"), exist_ok=True)
    gw = Gateway(cfg, data, ws, dsh_factory=factory)
    serve(gw, cfg["bind"], cfg["port"])


if __name__ == "__main__":
    main()
