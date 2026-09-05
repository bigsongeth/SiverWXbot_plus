# -*- coding: utf-8 -*-
"""把「一次 AI 回复」改成「问一次肥肉大脑」。

BrainAPI 对外长得和 wxbot_core 的四个接口类一样（.chat(message, prompt=, history=) -> str），
所以机器人的整条回复链路（历史/分条/接话闸门/故障转移）一行不改：
- 大脑说话 → 多条气泡用上游的 SPLIT_SEPARATOR 拼起来，交给 _parse_split_reply 分条发；
- 大脑不接话 → 返回 [NO_REPLY]，交给上游的 apply_no_reply_gate 静默；
- 大脑出错/超时/连不上 → 返回 model_fallback 认的那个固定失败串，让故障转移链换老接口顶上。
刻意不 import wxbot_core（会连带拉起 wxautox），常量在这里各写一份，与上游保持一致。
"""
from __future__ import annotations

import json
import logging
import re

import requests

SPLIT_SEPARATOR = "||SPLIT||"            # = wxbot_core.SPLIT_SEPARATOR
NO_REPLY_TOKEN = "[NO_REPLY]"            # = wxbot_core.NO_REPLY_TOKEN
API_ERROR_TEXT = "API返回错误，请稍后再试"   # = plugins/model_fallback/chain.py API_ERROR_TEXT

_SENDER = re.compile(r"^([^:：\n]{1,30})[:：]\s*")
_log = logging.getLogger("dsh_brain")

# 绕开系统代理：这台 Windows 的 IE 代理指向局域网某台机器（CLAUDE.md 3.12），Tailscale 地址进代理必挂
HTTP = requests.Session()
HTTP.trust_env = False


def split_sender(message: str):
    """群消息是 `发言人: 内容`（wxbot_core 的 content_with_sender），拆出来给大脑；私聊没有前缀。"""
    m = _SENDER.match(message or "")
    if m:
        return m.group(1).strip(), message[m.end():].strip()
    return "", (message or "").strip()


class BrainAPI:
    def __init__(self, conversation: str, is_group: bool, gateway_url: str, timeout_sec: float = 300):
        self.conversation = conversation
        self.is_group = is_group
        self.gateway_url = gateway_url.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        # model_fallback 按 (base_url, model, key 前 8 位) 去重，给它能认的属性
        self.base_url = self.gateway_url
        self.DS_NOW_MOD = "feirou-brain"
        self.api_key = ""

    def chat(self, message, model=None, stream=False, prompt=None, history=None,
             image_path: str = "", image_url: str = ""):
        text = str(message or "")
        if self.is_group:
            sender, text = split_sender(text)
            sender = sender or self.conversation
        else:
            sender = self.conversation
        if image_path or image_url:
            text = (text + " [图片]").strip()
        payload = {"conversation": self.conversation, "is_group": self.is_group, "sender": sender,
                   "text": text, "prime": list(history or [])}
        try:
            r = HTTP.post(self.gateway_url + "/reply", json=payload, timeout=self.timeout_sec)
        except Exception as e:
            _log.warning("dsh_brain: 网关不可达 %s: %s", type(e).__name__, e)
            return API_ERROR_TEXT
        if r.status_code != 200:
            _log.warning("dsh_brain: 网关 HTTP %s: %s", r.status_code, r.text[:200])
            return API_ERROR_TEXT
        try:
            out = r.json()
        except ValueError:
            _log.warning("dsh_brain: 网关返回不是 JSON: %s", r.text[:200])
            return API_ERROR_TEXT
        if out.get("bubbles"):
            return SPLIT_SEPARATOR.join(str(b) for b in out["bubbles"] if str(b).strip())
        if out.get("no_reply"):
            return NO_REPLY_TOKEN
        _log.warning("dsh_brain: 网关返回错误 %s", json.dumps(out, ensure_ascii=False)[:200])
        return API_ERROR_TEXT
