# -*- coding: utf-8 -*-
"""验证 dsh sdk 运行时能不能【同时】跑多个 session（2026-09-08）。

背景：网关现在全局串行（Gateway.lock + 单个 self.dsh + 单槽 self.inflight），一条慢就把
所有会话堵住（生产实测中位 57 秒、p90 177 秒）。要放开并发，最底下那块未知是：
dsh 自己允不允许两轮同时在飞？线路层面 DshClient 已经按 sessionId 分队列了
（dsh_client.py 的 _notes / _write_lock），但运行时行为没人测过。

怎么跑（在 mac-mini 上，代码副本 ~/feirou-brain/app）：
    cd ~/feirou-brain/app && . ~/feirou-brain/env && export SONGKEY_API_KEY \
        && /usr/bin/python3 brain/verify/concurrent_sessions.py

★ 用的是生产那一套：生产的 cordis.patch.yml（人设/模型/技能/MCP 全一样）、生产的 dsh-home、
  生产的 workspace 当 cwd。只有一处不同 —— FEIROU_GW 指向本脚本起的 stub，不打生产网关。
  必须这么隔离：server.py 的 tool_call 在 wx_reply 【不带 turn_id】时是放行的，
  万一生产此刻正在处理一条真消息，测试进程的话会被当成回复发进真实微信群。
  stub 同时充当取证点：wx_reply 带的 turn_id 能不能把两轮分开，正好一起验了。

完成判据（先定死，别事后找补）：
  A 能并发：两个线程同时 prompt 不同 sessionId，两轮都在超时内正常收尾（turn/end + idle），error 为空
  B 不串台：各自回答里只出现自己的暗号
  C 真重叠：并发墙钟 < 串行两轮之和 × 0.75（否则说明 dsh 内部还是排队）
  D 可路由：stub 收到两次 wx_reply，turn_id 各自对得上
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
STUB_PORT = int(os.environ.get("STUB_PORT", "8599"))
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "180"))

# 暗号：各自只该出现在自己那一轮的回答里
MARKS = {"A": "苹果苹果", "B": "香蕉香蕉"}

_calls = []          # stub 收到的工具调用：{ts, name, turn_id, args}
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
    """贴近生产的消息体：带轮次盖章、要求走 wx_reply 收尾，但明确禁止调别的工具和写文件。"""
    return (
        "[并发压测 | 轮次:%s]\n"
        "这是一次程序化压测，不是真人在说话。请严格照两步做，别发挥、别读写任何文件：\n"
        "第一步：立刻调用 kb_search，query 就写这七个字：压测打点%s\n"
        "        （这一步只是打时间戳，返回什么都不用管，不要因为它的结果改变第二步。）\n"
        "第二步：调用 wx_reply，bubbles 只放一条，内容就是这四个字：%s，并带上 turn_id=%s。\n"
        "除这两个工具外不要调用任何其它工具。\n"
        % (turn_id, tag, MARKS[tag], turn_id)
    )


def run_turn(dsh, tag: str, session_id: str, turn_id: str, out: dict) -> None:
    t0 = time.time()
    res = dsh.prompt(session_id, build_msg(tag, turn_id), TURN_TIMEOUT)
    out[tag] = {"t0": t0, "t1": time.time(), "sec": round(time.time() - t0, 1),
                "timed_out": res.timed_out, "error": res.error,
                "text": res.text, "reasoning_len": len(res.reasoning),
                "events": [e.get("type") for e in res.events],
                "session_id": session_id, "turn_id": turn_id}


def marks_in(s: str):
    return sorted(k for k, v in MARKS.items() if v in (s or ""))


def main() -> None:
    if not os.environ.get("SONGKEY_API_KEY"):
        sys.exit("SONGKEY_API_KEY 未设置（先 . ~/feirou-brain/env && export SONGKEY_API_KEY）")
    for p in (PATCH, WS, NODE, DSH_BIN):
        if not os.path.exists(p):
            sys.exit("缺文件：%s" % p)

    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), make_stub())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("[stub] 监听 127.0.0.1:%d（工具回调全部落到这里，不碰生产网关）" % STUB_PORT, flush=True)

    env = dict(os.environ, HOME=DATA, DSH_HOME=os.path.join(DATA, "dsh-home"),
               FEIROU_GW="http://127.0.0.1:%d" % STUB_PORT)
    dsh = DshClient(profile.dsh_argv(NODE, DSH_BIN, PATCH), cwd=WS, env=env,
                    stderr_path=os.path.join(DATA, "log", "dsh-concurrency-test.err"))
    epoch = uuid.uuid4().hex[:6]
    print("[dsh] 启动…（生产 patch + 生产 workspace，epoch=%s）" % epoch, flush=True)
    dsh.start()
    t = time.time()
    cfg = config.load(DATA)   # provider/model 也用生产的，别写死（生产已从 songkey-auto 换成 deepseek-v4-flash）
    dsh.initialize(WS, cfg["provider"], cfg["model"])
    print("[dsh] initialize ok %.1fs pid=%s model=%s" % (time.time() - t, dsh.p.pid, cfg["model"]), flush=True)

    report = {}

    # ---- 阶段 1：串行基线 ----
    print("\n=== 阶段 1：串行两轮（基线）===", flush=True)
    ser = {}
    ser_t0 = time.time()
    for tag in ("A", "B"):
        run_turn(dsh, tag, "压测串行%s#%s" % (tag, epoch), "ser" + tag + epoch, ser)
        r = ser[tag]
        print("  %s %ss timeout=%s error=%r 暗号=%s" % (tag, r["sec"], r["timed_out"],
                                                       r["error"], marks_in(r["text"])), flush=True)
    ser_wall = round(time.time() - ser_t0, 1)
    print("  串行墙钟合计 %ss" % ser_wall, flush=True)

    with _calls_lock:
        calls_after_serial = len(_calls)

    # ---- 阶段 2：并发 ----
    print("\n=== 阶段 2：两个 session 同时 prompt ===", flush=True)
    con = {}
    ths = []
    con_t0 = time.time()
    for tag in ("A", "B"):
        th = threading.Thread(target=run_turn,
                              args=(dsh, tag, "压测并发%s#%s" % (tag, epoch), "con" + tag + epoch, con),
                              name="turn-" + tag)
        ths.append(th)
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    con_wall = round(time.time() - con_t0, 1)
    for tag in ("A", "B"):
        r = con.get(tag)
        if not r:
            print("  %s 线程没产出结果" % tag, flush=True)
            continue
        print("  %s %ss timeout=%s error=%r 暗号=%s" % (tag, r["sec"], r["timed_out"],
                                                       r["error"], marks_in(r["text"])), flush=True)
    print("  并发墙钟合计 %ss（串行 %ss）" % (con_wall, ser_wall), flush=True)

    # 真重叠？两轮的 [t0,t1] 区间有交集才算
    overlap = 0.0
    if "A" in con and "B" in con:
        a, b = con["A"], con["B"]
        overlap = round(max(0.0, min(a["t1"], b["t1"]) - max(a["t0"], b["t0"])), 1)
    print("  两轮时间区间重叠 %ss" % overlap, flush=True)

    with _calls_lock:
        con_calls = _calls[calls_after_serial:]
        all_calls = list(_calls)

    # ---- 判据 ----
    print("\n=== 判据 ===", flush=True)
    ok_a = all(tag in con and not con[tag]["timed_out"] and not con[tag]["error"] for tag in ("A", "B"))
    ok_b = all(marks_in(con.get(tag, {}).get("text", "")) in ([tag], []) for tag in ("A", "B"))
    # B 更严格的判据放到工具调用上（模型常常只在 wx_reply 里说话，res.text 可能为空）
    tid_ok = {}
    for tag in ("A", "B"):
        want_tid = "con" + tag + epoch
        mine = [c for c in con_calls if c["name"] == "wx_reply" and c["turn_id"] == want_tid]
        blob = json.dumps([c["args"].get("bubbles") for c in mine], ensure_ascii=False)
        tid_ok[tag] = {"次数": len(mine), "暗号": marks_in(blob)}
    ok_d = all(tid_ok[t]["次数"] >= 1 and tid_ok[t]["暗号"] == [t] for t in ("A", "B"))
    no_tid = [c for c in con_calls if c["name"] in ("wx_reply", "no_reply") and not c["turn_id"]]
    # ★ 真并行的硬证据：各轮取 [第一次工具调用, wx_reply] 作为「确实在推进」的区间，两区间有交集
    #   才叫真并行。只看墙钟是不够的——dsh 若内部排队，后一轮的计时里前半段是排队，
    #   照样会显示成「区间重叠」。
    span = {}
    for tag in ("A", "B"):
        mine = [c for c in con_calls
                if (c["name"] == "kb_search" and tag in json.dumps(c["args"], ensure_ascii=False))
                or (c["name"] == "wx_reply" and c["turn_id"] == "con" + tag + epoch)]
        if mine:
            span[tag] = (min(c["ts"] for c in mine), max(c["ts"] for c in mine))
    work_overlap = 0.0
    if len(span) == 2:
        work_overlap = round(max(0.0, min(span["A"][1], span["B"][1]) - max(span["A"][0], span["B"][0])), 1)
    ok_c = work_overlap > 0

    print("  A 能并发（两轮都正常收尾）: %s" % ok_a, flush=True)
    print("  B 不串台（回答不混）      : %s" % ok_b, flush=True)
    print("  C 真并行（干活区间有交集）: %s  [干活重叠 %ss；粗区间重叠 %ss；并发墙钟 %ss vs 串行 %ss]"
          % (ok_c, work_overlap, overlap, con_wall, ser_wall), flush=True)
    for tag in ("A", "B"):
        if tag in span:
            print("     %s 干活区间 %.1fs → %.1fs（相对并发起点）"
                  % (tag, span[tag][0] - con_t0, span[tag][1] - con_t0), flush=True)
    print("  D 可路由（turn_id 对得上）: %s  %s" % (ok_d, json.dumps(tid_ok, ensure_ascii=False)), flush=True)
    print("  并发期没带 turn_id 的收尾调用: %d 次%s"
          % (len(no_tid), "（★ 并发下这类调用无法归属，改造时必须堵死）" if no_tid else ""), flush=True)
    print("\n  结论：%s" % ("dsh 支持并发多 session" if (ok_a and ok_d and ok_c)
                            else "dsh 并发不成立或未真重叠，见上面各项"), flush=True)

    dump = os.path.join(DATA, "log", "concurrency-test-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(dump, "w", encoding="utf-8") as f:
        json.dump({"epoch": epoch, "serial": ser, "serial_wall": ser_wall,
                   "concurrent": con, "concurrent_wall": con_wall, "overlap_sec": overlap, "work_overlap_sec": work_overlap,
                   "verdict": {"A": ok_a, "B": ok_b, "C": ok_c, "D": ok_d},
                   "tool_calls": all_calls}, f, ensure_ascii=False, indent=2)
    print("  明细已存 %s" % dump, flush=True)

    dsh.stop()
    srv.shutdown()


if __name__ == "__main__":
    main()
