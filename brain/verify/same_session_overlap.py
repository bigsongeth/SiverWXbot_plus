# -*- coding: utf-8 -*-
"""验证「网关判超时之后，同一个 session 还能不能接着聊」（2026-09-09）。

为什么要测：`restart_after_timeouts` 现在等效为 1，一轮超时就把整个 dsh 进程杀掉重建，
下一轮换新 session、重新灌历史（生产日志里 `primed:true`），白花几十秒冷启动。把它调大
就能省下这段——**前提是超时那轮的残留不会污染下一轮**。dsh sdk 至今没有 session/cancel
（dsh_client.py 里那条注释，09-05 实测、09-08 复核），所以判超时那一刻，上一轮其实还在
后台跑，迟早会吐出 turn/end 和它自己的 wx_reply。

具体怀疑点（读 dsh_client.prompt 读出来的，不是猜的）：
    q = self._session_queue(session_id)
    while not q.empty(): q.get_nowait()      # ← 只清「此刻队列里有的」
    ...
    elif meth == "session.status" and status == "idle" and turn_ended: return res
上一轮的 turn/end 如果**恰好在这次清空之后**才到达，本轮就会把它当成自己的收尾，
配上随后的 idle 立刻返回一个空结果——表现是「第二问秒回、答非所问」。清队列拦得住
已经躺在队列里的旧事件，拦不住正在路上的。

怎么造出「超时但还在飞」：把第一轮的 turn_timeout 设成 8 秒（模型光首字节就不止），
网关侧判超时返回，dsh 那边照旧跑完。这和生产里的情形是一码事。

跑法（mac-mini，机器人/网关可以照常跑着，本脚本另起自己的 dsh 进程 + stub 网关）：
    cd ~/feirou-brain/app && . ~/feirou-brain/env && export SONGKEY_API_KEY \
        && /usr/bin/python3 brain/verify/same_session_overlap.py

★ 和 concurrent_sessions.py 一样用生产那一套（patch/dsh-home/workspace），只把 FEIROU_GW
  指向本地 stub —— 绝不能让测试的 wx_reply 打进生产网关发到真实微信。

完成判据（先定死，别事后找补）：
  A 第二轮正常收尾：turn/end + idle，error 为空、没超时
  B 不串台：第二轮的回答和它的 wx_reply 里只出现自己的暗号（香蕉香蕉）
  C 不是被上一轮的事件提前唤醒：第二轮耗时 >= MIN_HONEST_SEC（真跑了一轮的下限）
  D 迟到可辨认：第一轮迟到的 wx_reply 带的是第一轮的 turn_id（生产靠它走 STALE_TURN 拒掉）

★ 2026-09-09 首跑结论（明细 log/overlap-test-20260909-002220.json）：**A/C/D 过，B 不过，
  但可以上生产。** 第二轮的 `res.text` 里确实混进了第一轮的暗号（上面那个怀疑点是真的，
  旧事件确实会被下一轮吸收），可是它自己的 `wx_reply` 干净、第一轮迟到的那次带的是
  第一轮的 turn_id。而 `server.py` 里 **`turn.text` 只写进日志的 `draft` 字段**，发出去的
  内容一律取自 `inf.result`（wx_reply 回调）—— 所以串的是日志，不是回复。
  据此把生产 `restart_after_timeouts` 设成 3（原先等效为 1，一超时就杀进程重灌历史）。
  代价写清楚：**超时之后那一轮的 `draft` 和 `dsh_tools` 两个日志字段会混入上一轮的残留**，
  排障时别把它们当成本轮的事实。要改就得给 dsh_client.prompt 按 turn 过滤事件，
  不是调这个配置能解决的。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from brain.gateway.dsh_client import DshClient  # noqa: E402
from brain.gateway import config, profile  # noqa: E402

DATA = os.path.expanduser(os.environ.get("FEIROU_DATA", "~/feirou-brain-data"))
WS = os.path.join(DATA, "workspace")
PATCH = os.path.join(DATA, "cordis.patch.yml")
NODE = os.path.expanduser(os.environ.get("NODE", "~/.nvm/versions/node/v24.19.0/bin/node"))
DSH_BIN = os.path.expanduser(os.environ.get(
    "DSH_BIN", "~/feirou-brain/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js"))
STUB_PORT = int(os.environ.get("STUB_PORT", "8598"))

SHORT_TIMEOUT = float(os.environ.get("SHORT_TIMEOUT", "8"))     # 第一轮：故意不够用
LONG_TIMEOUT = float(os.environ.get("LONG_TIMEOUT", "150"))     # 第二轮：给足
MIN_HONEST_SEC = float(os.environ.get("MIN_HONEST_SEC", "5"))   # 低于这个就是被旧事件唤醒的假收尾
SETTLE_SEC = float(os.environ.get("SETTLE_SEC", "90"))          # 结尾再等一会儿，收第一轮的迟到回调

MARKS = {"A": "苹果苹果", "B": "香蕉香蕉"}

_calls = []
_calls_lock = threading.Lock()


def make_stub():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                args = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                args = {}
            name = self.path[len("/tool/"):] if self.path.startswith("/tool/") else self.path
            with _calls_lock:
                _calls.append({"ts": time.time(), "name": name,
                               "turn_id": args.get("turn_id"), "args": args})
            if name == "wx_reply":
                out = {"ok": True, "text": "已发送 %d 条。本轮到此为止，不要再调用 wx_reply。"
                       % len(args.get("bubbles") or [])}
            elif name == "no_reply":
                out = {"ok": True, "text": "好，这条不接。"}
            else:
                out = {"ok": True, "text": "（压测 stub）本次不提供真实结果，直接按已知信息回复即可。"}
            body = json.dumps(out, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            pass
    return H


def build_msg(tag: str, turn_id: str) -> str:
    return (
        "[重叠压测 | 轮次:%s]\n"
        "这是一次程序化压测，不是真人在说话。请严格照两步做，别发挥、别读写任何文件：\n"
        "第一步：立刻调用 kb_search，query 就写这七个字：压测打点%s\n"
        "        （这一步只是打时间戳，返回什么都不用管，不要因为它的结果改变第二步。）\n"
        "第二步：调用 wx_reply，bubbles 只放一条，内容就是这四个字：%s，并带上 turn_id=%s。\n"
        "除这两个工具外不要调用任何其它工具。\n"
        % (turn_id, tag, MARKS[tag], turn_id)
    )


def marks_in(s: str):
    return sorted(k for k, v in MARKS.items() if v in (s or ""))


def calls_blob(calls, turn_id=None, name=None):
    sel = [c for c in calls
           if (turn_id is None or c["turn_id"] == turn_id) and (name is None or c["name"] == name)]
    return sel, json.dumps([c["args"] for c in sel], ensure_ascii=False)


def main() -> None:
    if not os.environ.get("SONGKEY_API_KEY"):
        sys.exit("SONGKEY_API_KEY 未设置（先 . ~/feirou-brain/env && export SONGKEY_API_KEY）")
    for p in (PATCH, WS, NODE, DSH_BIN):
        if not os.path.exists(p):
            sys.exit("缺文件：%s" % p)

    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), make_stub())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("[stub] 监听 127.0.0.1:%d（工具回调全落这里，不碰生产网关）" % STUB_PORT, flush=True)

    env = dict(os.environ, HOME=DATA, DSH_HOME=os.path.join(DATA, "dsh-home"),
               FEIROU_GW="http://127.0.0.1:%d" % STUB_PORT)
    dsh = DshClient(profile.dsh_argv(NODE, DSH_BIN, PATCH), cwd=WS, env=env,
                    stderr_path=os.path.join(DATA, "log", "dsh-overlap-test.err"))
    epoch = uuid.uuid4().hex[:6]
    print("[dsh] 启动…（生产 patch + 生产 workspace，epoch=%s）" % epoch, flush=True)
    dsh.start()
    t = time.time()
    cfg = config.load(DATA)
    dsh.initialize(WS, cfg["provider"], cfg["model"])
    print("[dsh] initialize ok %.1fs pid=%s model=%s" % (time.time() - t, dsh.p.pid, cfg["model"]), flush=True)

    # 同一个 session 走完两轮 —— 这正是「不重建进程」时生产会做的事
    sid = "重叠压测#%s" % epoch
    tid1, tid2 = "ovl1" + epoch, "ovl2" + epoch

    print("\n=== 第一轮：turn_timeout=%ss，故意不够用（模拟网关判超时）===" % SHORT_TIMEOUT, flush=True)
    t0 = time.time()
    r1 = dsh.prompt(sid, build_msg("A", tid1), SHORT_TIMEOUT)
    sec1 = round(time.time() - t0, 1)
    print("  %ss timeout=%s error=%r events=%d" % (sec1, r1.timed_out, r1.error, len(r1.events)), flush=True)
    if not r1.timed_out:
        print("  ⚠️ 第一轮没超时（模型这次太快），本次跑不出重叠场景 —— 调小 SHORT_TIMEOUT 重来", flush=True)

    with _calls_lock:
        calls_after_1 = len(_calls)

    print("\n=== 第二轮：同一个 session 接着问（不重建进程、不换 session）===", flush=True)
    t0 = time.time()
    r2 = dsh.prompt(sid, build_msg("B", tid2), LONG_TIMEOUT)
    sec2 = round(time.time() - t0, 1)
    print("  %ss timeout=%s error=%r 暗号=%s events=%d"
          % (sec2, r2.timed_out, r2.error, marks_in(r2.text), len(r2.events)), flush=True)

    print("\n=== 静置 %ss，收第一轮的迟到回调 ===" % SETTLE_SEC, flush=True)
    time.sleep(SETTLE_SEC)

    with _calls_lock:
        all_calls = list(_calls)
        during_2 = _calls[calls_after_1:]

    c1, _ = calls_blob(all_calls, turn_id=tid1, name="wx_reply")
    c2, blob2 = calls_blob(all_calls, turn_id=tid2, name="wx_reply")
    no_tid = [c for c in all_calls if c["name"] in ("wx_reply", "no_reply") and not c["turn_id"]]

    print("\n=== 判据 ===", flush=True)
    ok_a = (not r2.timed_out) and (not r2.error)
    # 第二轮说的话只该有自己的暗号：res.text 和它自己那些 wx_reply 都算
    ok_b = marks_in(r2.text) in (["B"], []) and marks_in(blob2) in (["B"], [])
    ok_c = sec2 >= MIN_HONEST_SEC
    ok_d = len(c1) == 0 or all(c["turn_id"] == tid1 for c in c1)

    print("  A 第二轮正常收尾        : %s  [%ss timeout=%s error=%r]" % (ok_a, sec2, r2.timed_out, r2.error), flush=True)
    print("  B 不串台                : %s  [res.text 暗号=%s；自己的 wx_reply 暗号=%s]"
          % (ok_b, marks_in(r2.text), marks_in(blob2)), flush=True)
    print("  C 不是被旧事件提前唤醒  : %s  [耗时 %ss，下限 %ss]" % (ok_c, sec2, MIN_HONEST_SEC), flush=True)
    print("  D 迟到回调带得出 turn_id: %s  [第一轮迟到的 wx_reply %d 次，全部带 %s]"
          % (ok_d, len(c1), tid1), flush=True)
    print("  第二轮期间收到的工具调用: %s"
          % json.dumps([{"name": c["name"], "turn_id": c["turn_id"]} for c in during_2], ensure_ascii=False), flush=True)
    print("  没带 turn_id 的收尾调用 : %d 次%s"
          % (len(no_tid), "（★ 这类在生产上无法归属，会被当成当前轮）" if no_tid else ""), flush=True)

    verdict = ok_a and ok_b and ok_c and ok_d
    print("\n  结论：%s" % ("超时后同 session 可以接着聊 → restart_after_timeouts 可以调大"
                            if verdict else
                            "★ 有污染，不能只靠调大 restart_after_timeouts，见上面不过的项"), flush=True)

    dump = os.path.join(DATA, "log", "overlap-test-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(dump, "w", encoding="utf-8") as f:
        json.dump({"epoch": epoch, "session_id": sid,
                   "turn1": {"sec": sec1, "timed_out": r1.timed_out, "error": r1.error,
                             "text": r1.text, "events": [e.get("type") for e in r1.events]},
                   "turn2": {"sec": sec2, "timed_out": r2.timed_out, "error": r2.error,
                             "text": r2.text, "events": [e.get("type") for e in r2.events]},
                   "verdict": {"A": ok_a, "B": ok_b, "C": ok_c, "D": ok_d},
                   "tool_calls": all_calls}, f, ensure_ascii=False, indent=2)
    print("  明细已存 %s" % dump, flush=True)

    dsh.stop()
    srv.shutdown()


if __name__ == "__main__":
    main()
