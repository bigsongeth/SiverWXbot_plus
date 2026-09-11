# -*- coding: utf-8 -*-
"""把「一次 AI 回复」改成「问一次肥肉大脑」。

BrainAPI 对外长得和 wxbot_core 的四个接口类一样（.chat(message, prompt=, history=) -> str），
所以机器人的整条回复链路（历史/分条/接话闸门/故障转移）一行不改：
- 大脑说话 → 多条气泡用上游的 SPLIT_SEPARATOR 拼起来，交给 _parse_split_reply 分条发；
- 大脑不接话 → 返回 [NO_REPLY]，交给上游的 apply_no_reply_gate 静默；
- 大脑出错/超时/连不上 → 隔几秒再起一轮让大脑重试（2026-09-07 用户拍板：别切备用接口，
  失败了就起新的一轮继续试）；重试也耗尽就回 exhausted_reply 那句固定话。exhausted_reply 留空才退回
  老行为：返回 model_fallback 认的失败串，让故障转移链换老接口顶上。
刻意不 import wxbot_core（会连带拉起 wxautox），常量在这里各写一份，与上游保持一致。
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

SPLIT_SEPARATOR = "||SPLIT||"            # = wxbot_core.SPLIT_SEPARATOR
NO_REPLY_TOKEN = "[NO_REPLY]"            # = wxbot_core.NO_REPLY_TOKEN
API_ERROR_TEXT = "API返回错误，请稍后再试"   # = plugins/model_fallback/chain.py API_ERROR_TEXT

_SENDER = re.compile(r"^([^:：\n]{1,30})[:：]\s*")


def _warn(msg: str) -> None:
    """失败原因走项目统一的 log()，落进面板日志流。

    2026-09-07：原来用 stdlib logging，只打到 stderr（Windows 上被重定向进
    panel_logs/panel_restart.log，中文还是 GBK 乱码），而 model_fallback 那句
    「具体原因见紧邻的上一条接口日志」说的是面板日志 —— 那里一个字都没有，
    用户只看到「主接口 BrainAPI 失败」却查不到为什么。
    logger.py 只依赖标准库，不会像 wxbot_core 那样连带拉起 wxautox，mac 上照样能跑单测。
    """
    try:
        from logger import log
        log(level="WARNING", message=f"[dsh_brain] {msg}")
    except Exception:
        logging.getLogger("dsh_brain").warning(msg)

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
    def __init__(self, conversation: str, is_group: bool, gateway_url: str, timeout_sec: float = 300,
                 max_attempts: int = 1, retry_delay_sec: float = 0, exhausted_reply: str = ""):
        self.conversation = conversation
        self.is_group = is_group
        self.gateway_url = gateway_url.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        # 失败后再起几轮：直接构造默认 1 次不重试、失败串交给故障转移（老行为，单测靠它）；
        # 插件从 config.json 传进来的默认是 2 次 + 固定话（store.DEFAULT_CONFIG）。
        self.max_attempts = max(1, int(max_attempts or 1))
        self.retry_delay_sec = max(0.0, float(retry_delay_sec or 0))
        self.exhausted_reply = str(exhausted_reply or "").strip()
        # model_fallback 按 (base_url, DS_NOW_MOD, key 前 8 位) 认接口身份（chain.api_identity），
        # 给它能认的属性。
        # ★★ DS_NOW_MOD 必须带上会话名，别改回固定串（2026-09-09 查出的生产事故）：
        # 这三个属性原来对所有会话都是同一个值，于是 model_fallback.wrap() 的 _wrap_cache
        # （key = (id(bot), api_identity(api))）把**所有会话的 BrainAPI 当成同一个接口**，
        # 命中缓存后只更新 _session_name（那只是日志字段），真正被调用的还是第一个会话的实例。
        # 后果：机器人每次重启后，第一个用大脑的会话会「劫持」之后所有会话 —— 群消息被当私聊、
        # 挂到别人的 conversation 上，记忆/人设/技能匹配/气泡上限全用错。
        # 实证（网关 replies 日志）：09-08 晚上所有群消息都记成私聊「青猫_🐕」；
        # 09-07 14:22 机器人重启后又全部锁定到「📈🐶」。从 dsh_brain 上线（09-06）就存在。
        self.base_url = self.gateway_url
        self.DS_NOW_MOD = "feirou-brain:%s:%s" % ("群" if is_group else "私", self.conversation)
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
        # 机器人的 memory_context_count 可能是 1000，网关预热只取最后 20 条，整包发纯属浪费
        payload = {"conversation": self.conversation, "is_group": self.is_group, "sender": sender,
                   "text": text, "prime": list(history or [])[-60:]}
        for attempt in range(1, self.max_attempts + 1):
            payload["attempt"] = attempt   # 网关据此在重试那轮提示大脑「上一轮超时了，少调工具」
            ok, reply = self._ask_once(payload)
            if ok:
                return reply
            if attempt < self.max_attempts:
                _warn(f"第 {attempt}/{self.max_attempts} 轮失败：{reply}，{self.retry_delay_sec:g} 秒后再起一轮")
                if self.retry_delay_sec:
                    time.sleep(self.retry_delay_sec)
                continue
            _warn(f"第 {attempt}/{self.max_attempts} 轮失败：{reply}，"
                  + ("回固定话，不切备用接口" if self.exhausted_reply else "交给故障转移链"))
        return self.exhausted_reply or API_ERROR_TEXT

    def _ask_once(self, payload: dict):
        """问网关一轮。返回 (成功?, 回复或失败原因)。成功包括「不接话」。"""
        try:
            r = HTTP.post(self.gateway_url + "/reply", json=payload, timeout=self.timeout_sec)
        except Exception as e:
            return False, f"网关不可达 {type(e).__name__}: {e}"
        if r.status_code != 200:
            return False, f"网关 HTTP {r.status_code}: {r.text[:200]}"
        try:
            out = r.json()
        except ValueError:
            return False, f"网关返回不是 JSON: {r.text[:200]}"
        if out.get("bubbles"):
            return True, SPLIT_SEPARATOR.join(str(b) for b in out["bubbles"] if str(b).strip())
        if out.get("no_reply"):
            return True, NO_REPLY_TOKEN
        return False, f"网关返回错误 {json.dumps(out, ensure_ascii=False)[:200]}"
