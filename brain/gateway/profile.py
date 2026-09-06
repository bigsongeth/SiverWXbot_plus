# -*- coding: utf-8 -*-
"""渲染 dsh sdk profile 的 patch 覆盖层，准备一个干净的 DSH_HOME。

为什么不用 !!js 在 yaml 里读文件：patch 由我们渲染更可控，也不依赖 dsh 的表达式求值上下文。
DSH_HOME 必须是大脑专用的空目录：dsh 会读 $DSH_HOME/AGENTS.md 和 $DSH_HOME/skills，
用本机 ~/.dsh 会把别的项目的说明带进肥肉的脑子（2026-09-05 实测踩到）。
"""
from __future__ import annotations
import os
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
TMPL_DIR = os.path.join(os.path.dirname(HERE), "profile")

_GROK_BLOCK = """    - id: mcp-grok
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: grok
        transport: stdio
        command: __NODE__
        args:
          - __SERVER_JS__
        env:
          SONGKEY_API_KEY: __KEY__
        toolCallTimeoutMs: 120000
        failOnStartupError: false
"""


def _read(name: str) -> str:
    with open(os.path.join(TMPL_DIR, name), encoding="utf-8") as f:
        return f.read()


def render_patch(persona: str, feirou_mcp_argv: List[str], grok_server_js: Optional[str],
                 songkey_key: str, node: str = "node") -> str:
    tmpl = _read("cordis.patch.yml.tmpl")
    persona_block = "\n".join("      " + ln for ln in persona.strip("\n").split("\n"))
    args_block = "\n".join("          - " + a for a in feirou_mcp_argv[1:])
    grok = ""
    if grok_server_js:
        grok = (_GROK_BLOCK.replace("__NODE__", node).replace("__SERVER_JS__", grok_server_js)
                .replace("__KEY__", songkey_key))
    return (tmpl.replace("__PERSONA__", persona_block)
            .replace("__FEIROU_CMD__", feirou_mcp_argv[0])
            .replace("__FEIROU_ARGS__", args_block)
            .replace("__GROK_BLOCK__\n", grok))


def prepare_dsh_home(dsh_home: str, songkey_key: str, model: str, skills_dir: Optional[str] = None) -> None:
    """建 DSH_HOME：settings.yaml 只写一次；`skills` 指向工作区的技能目录。

    dsh 的技能目录只认 <项目>/.dsh/skills、<项目>/.agents/skills、$DSH_HOME/skills、~/.agents/skills，
    工作区里的 skills/ 它不扫（2026-09-06 实测模型说"catalog 里没有 hz-food-map"，只能自己 glob+read，
    一轮多花几十秒还常读不到）。所以把 $DSH_HOME/skills 做成指向工作区 skills/ 的软链。
    """
    os.makedirs(dsh_home, exist_ok=True)
    settings = os.path.join(dsh_home, "settings.yaml")
    if not os.path.exists(settings):
        with open(settings, "w", encoding="utf-8") as f:
            f.write(_read("settings.yaml.tmpl").replace("__MODEL__", model))
    if skills_dir:
        link = os.path.join(dsh_home, "skills")
        if os.path.islink(link) and os.readlink(link) != skills_dir:
            os.unlink(link)
        if not os.path.exists(link) and not os.path.islink(link):
            os.symlink(skills_dir, link)


def bridge_gateway_url(bind: str, port: int) -> str:
    """MCP 桥回打网关用的地址。网关绑在具体地址（如 Tailscale IP）时 127.0.0.1 根本没在听——
    2026-09-06 第一次真机测试就栽在这：wx_reply 连不上网关，模型试三次后整轮超时，群里没人应。"""
    b = (bind or "").strip()
    host = "127.0.0.1" if b in ("", "0.0.0.0", "127.0.0.1", "localhost", "::") else b
    return f"http://{host}:{int(port)}"


def dsh_argv(node: str, dsh_bin: str, patch_path: str) -> List[str]:
    return [node, dsh_bin, "--profile", "sdk", "--patch", patch_path]
