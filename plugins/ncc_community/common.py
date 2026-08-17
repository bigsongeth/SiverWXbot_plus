# -*- coding: utf-8 -*-
"""ncc_community 插件公共工具：日志与统一回复。"""
from __future__ import annotations

import os
import sys
import time

# 机器人程序化回复的统一前缀。
# 指令解析层会忽略带此前缀的消息，保证机器人自己的回复（在管理群里
# 属于 self 消息，会重新进入回调）不会被当成指令二次处理。
REPLY_PREFIX = "🤖"

try:
    from logger import log as _log
except Exception:  # 单测环境没有项目根的 logger
    def _log(level="INFO", message=""):
        print(f"[{level}] {message}")


def log(level: str, message: str) -> None:
    # ★ 单测不许写进生产日志（2026-08-14 定）。`logger.log` 无条件 append 到
    # panel_logs/log_<日期>.txt，于是在项目根跑一次单测，就往生产日志里灌进一片
    # 「群1」「小明」「转发中止：微信 UI 锁未释放」—— 8-13 整晚看着像转发天天在崩，
    # 其实一次真实转发都没跑过，全是测试夹具。排查时真假分不开，代价极大。
    # 环境变量在【调用时】读，测试文件里 setdefault 一句即可，不挑导入顺序。
    if os.environ.get("NCC_LOG_SILENT") == "1":
        print(f"[{level}] [ncc_community] {message}")
        return
    try:
        _log(level=level, message=f"[ncc_community] {message}")
    except Exception:
        pass
    # 立即刷盘。logger.log_server 只是 print 到 stdout，而 bot 在后台跑时 stdout 是
    # 【带缓冲】的 —— 一条日志几十字节，要攒满几 KB 才落到 panel_logs。
    # 2026-08-04 排查转发卡死时在这上面栽了：超时逻辑到底跑没跑无从判断，因为该出现的
    # WARNING 还压在缓冲区里，我据此误判成"代码没执行"，白绕了一大圈。
    # 卡死类问题恰恰是"最后一行日志"最值钱，绝不能等缓冲区攒满。
    try:
        sys.stdout.flush()
    except Exception:
        pass


# 群发专用日志。★ 为什么要单独一份（2026-08-15 定）：
# `logger.log` 落到 panel_logs/log_<日期>.txt，而那个文件里混着【每一条微信消息】的
# 流水，一天几千行。群发出问题时要从里面把线索捞出来，实际做过一次，非常痛苦。
# 更要命的是转发路径原来【成功的群一行都不记】，只有异常才写 —— 86 个群跑下去，
# 日志里近乎空白：卡在谁身上、跑到第几个、每个群花了多久，事后全都无从查起。
FORWARD_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "panel_logs", "ncc_forward.log")


def flog(message: str, level: str = "INFO") -> None:
    """群发专用日志：正常日志照写一份（在线能看到），另外单独落盘一份（事后好查）。
    写盘失败一律吞掉——日志绝不能拖垮群发本身。"""
    log(level, message)
    # 单测同样不许落盘——刚给主日志堵上的洞，别在这儿又开一个
    if os.environ.get("NCC_LOG_SILENT") == "1":
        return
    try:
        os.makedirs(os.path.dirname(FORWARD_LOG), exist_ok=True)
        with open(FORWARD_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}\n")
    except OSError:
        pass


def reply(chat, text: str):
    """在会话内发送带前缀的机器人回复，失败只记日志不抛出。"""
    try:
        return chat.SendMsg(msg=f"{REPLY_PREFIX} {text}")
    except Exception as e:
        log("ERROR", f"回复失败: {e}")
        return None


def notify_admin(bot, cfg, text: str) -> None:
    """给管理群推一条带前缀的提醒（没配管理群或发送失败只记日志，不抛出）。

    用于"用户侧已经回过话、但需要人接手"的场景（拉群失败、发现新群…），
    避免用户在私聊里干等着问"管理员在哪啊"。"""
    try:
        admin = (cfg or {}).get("admin_group")
    except Exception:
        admin = None
    if not admin:
        return
    try:
        bot.wx.SendMsg(msg=f"{REPLY_PREFIX} {text}", who=admin)
    except Exception as e:
        log("ERROR", f"提醒管理群失败: {e}")


def is_bot_reply(text: str) -> bool:
    return (text or "").strip().startswith(REPLY_PREFIX)
