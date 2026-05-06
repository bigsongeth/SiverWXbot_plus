from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .store import CheckinStore, ClaimResult

TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "checkin.sqlite3"
TRIGGERS = {"签到", "打卡", "领码", "领取兑换码", "兑换码", "key", "KEY"}

def normalize_text(text: str) -> str:
    return re.sub(r"[\s!！。.,，~～]+", "", (text or "").strip())

def is_checkin_trigger(text: str) -> bool:
    return normalize_text(text) in TRIGGERS

def is_debug_trigger(text: str) -> bool:
    return normalize_text(text) in {"签到调试", "打卡调试"}

def build_user_key(chat, message) -> str:
    for obj in (chat, message):
        for name in ("wxid", "user_id", "sender_id"):
            value = getattr(obj, name, None)
            if value and not callable(value):
                return str(value)
    for obj in (message, chat):
        for name in ("sender", "who", "name"):
            value = getattr(obj, name, None)
            if value and not callable(value):
                return str(value)
    return ""

def build_user_name(chat, message) -> str:
    for obj in (message, chat):
        for name in ("sender", "who", "name"):
            value = getattr(obj, name, None)
            if value and not callable(value):
                return str(value)
    return ""

def is_private_chat(chat) -> bool:
    chat_type = str(getattr(chat, "chat_type", "") or "").lower()
    if chat_type and any(token in chat_type for token in ("group", "群", "room")):
        return False
    who = str(getattr(chat, "who", "") or "")
    return not who.endswith("@chatroom")

def format_expired_at(ts: int | None) -> str:
    if not ts:
        return "明早 8 点"
    return datetime.fromtimestamp(int(ts), TZ).strftime("%m-%d %H:%M")

def format_reply(result: ClaimResult) -> str:
    if result.status == "success":
        return f"签到成功 ✅\n今天给你抽到：{result.display_amount} {result.display_unit}\n兑换码：{result.code}\n有效期到：{format_expired_at(result.expired_at)}（北京时间）"
    if result.status == "already_claimed":
        return f"你今天已经领过啦 ✅\n额度：{result.display_amount} {result.display_unit}\n兑换码：{result.code}\n有效期到：{format_expired_at(result.expired_at)}（北京时间）"
    if result.status == "no_code":
        return "今天的兑换码暂时领完了，等补码后再来试一下。"
    return "签到暂时失败了，稍后再试一下。"

def handle_checkin(chat, message, text: str | None = None, db_path: str | Path | None = None):
    text = text if text is not None else str(getattr(message, "content", "") or getattr(message, "text", "") or "")
    if not is_private_chat(chat):
        return False, None
    if is_debug_trigger(text):
        safe = {"chat_who": str(getattr(chat, "who", "")), "chat_type": str(getattr(chat, "chat_type", "")), "chat_wxid": str(getattr(chat, "wxid", "")), "message_sender": str(getattr(message, "sender", "")), "message_id": str(getattr(message, "id", "")), "user_key": build_user_key(chat, message)}
        return True, "签到调试信息：" + "；".join(f"{k}={v}" for k, v in safe.items() if v)
    if not is_checkin_trigger(text):
        return False, None
    store = CheckinStore(db_path or DB_PATH)
    result = store.claim_code(build_user_key(chat, message), build_user_name(chat, message), text, str(getattr(message, "id", "") or ""))
    return True, format_reply(result)
