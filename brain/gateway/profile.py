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


def prepare_dsh_home(dsh_home: str, songkey_key: str, model: str) -> None:
    os.makedirs(dsh_home, exist_ok=True)
    settings = os.path.join(dsh_home, "settings.yaml")
    if not os.path.exists(settings):
        with open(settings, "w", encoding="utf-8") as f:
            f.write(_read("settings.yaml.tmpl").replace("__MODEL__", model))


def dsh_argv(node: str, dsh_bin: str, patch_path: str) -> List[str]:
    return [node, dsh_bin, "--profile", "sdk", "--patch", patch_path]
