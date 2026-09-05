# -*- coding: utf-8 -*-
"""肥肉大脑的 MCP 哑桥：4 个工具，逻辑全在网关，这里只转发。

为什么是哑桥：校验（预算/气泡数/反口头禅）需要知道"当前在回哪条消息"，只有网关知道；
MCP 服务由 dsh 拉起、跨会话共用，自己没法知道。所以这里一行业务逻辑都不能有。
stdout 只能写 JSON-RPC 帧；调试信息写 stderr。
"""
import json
import os
import sys
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

GW = os.environ.get("FEIROU_GW", "http://127.0.0.1:8500").rstrip("/")

# dsh 的 toolCallTimeoutMs 是 60000（见 brain/profile/cordis.patch.yml.tmpl），
# 这里必须留在它之下，不然 dsh 先超时、我们的重试/报错永远传不回去。
# kb_search 在网关侧封顶 20s，wx_reply 是本地写库基本秒回，45s 留了充足余量。
GW_TIMEOUT_SEC = 45

TURN_ID = {"type": "string", "description": "系统在消息开头给你的「轮次」标识，原样填入"}

TOOLS = [
    {"name": "wx_reply",
     "description": "把你决定要说的话发到微信。这是唯一的说话通道，正文里写的字不会被发出去。"
                    "群聊最多 2 条气泡、私聊最多 3 条，总字数要在系统给你的预算之内；像真人一样一两句话，不要客套收尾。"
                    "被拒绝时按返回的提示改了再调一次。",
     "inputSchema": {"type": "object", "properties": {
         "bubbles": {"type": "array", "items": {"type": "string"}, "description": "按顺序发出的气泡，每条一段话"},
         "turn_id": TURN_ID},
         "required": ["bubbles"]}},
    {"name": "no_reply",
     "description": "判断这条消息不需要接话时调用（附和、点赞、别人之间的闲聊、话题已经聊完）。调了它就不要再调 wx_reply。",
     "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}, "turn_id": TURN_ID}, "required": ["reason"]}},
    {"name": "propose_shared_knowledge",
     "description": "把聊天里得到的、对所有人都有用的事实（据点变动、价格、联系方式、活动）提交审核。"
                    "通过后才会进入共享知识；未通过前不要当作事实告诉别人。",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "一句话事实"},
         "source": {"type": "string", "description": "谁在哪说的"},
         "turn_id": TURN_ID}, "required": ["text", "source"]}},
    {"name": "kb_search",
     "description": "检索 NCC 社区知识库（公众号文章 + 固定事实清单）。问据点、活动、报名、主理人、社区历史时先查再答。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]
NAMES = {t["name"] for t in TOOLS}


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def call_gateway(name, args):
    req = urllib.request.Request(f"{GW}/tool/{name}", data=json.dumps(args, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=GW_TIMEOUT_SEC) as r:   # 直连网关，无视环境代理（见 gateway/kb.py 注释）
        return json.loads(r.read().decode("utf-8"))


def handle_frame(m):
    """处理一个已解析成 dict 的 JSON-RPC 帧。不抛异常——任何一帧出错都不能带崩整条 stdin 循环。"""
    meth, i = m.get("method"), m.get("id")
    p = m.get("params")
    if not isinstance(p, dict):
        p = {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": i, "result": {
            "protocolVersion": p.get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "feirou", "version": "1"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}})
    elif meth == "tools/call":
        name = p.get("name")
        args = p.get("arguments")
        if not isinstance(args, dict):
            args = {}
        if name not in NAMES:
            send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": f"未知工具 {name}"}], "isError": True}})
            return
        try:
            res = call_gateway(name, args)
            ok, text = bool(res.get("ok")), str(res.get("text", ""))
        except Exception as e:  # 网关不在 → 告诉模型，别让 dsh 挂
            ok, text = False, f"网关不可用：{e}"
        send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": text}], "isError": not ok}})
    elif meth == "ping":
        send({"jsonrpc": "2.0", "id": i, "result": {}})
    elif i is not None:
        send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": f"method not found: {meth}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if not isinstance(m, dict):
            # 合法 JSON 但顶层不是对象（数组批量请求 / 数字 / 字符串 / true / null）：
            # 我们不支持批量帧，跳过而不是让 .get() 抛 AttributeError 带崩整条桥。
            continue
        try:
            handle_frame(m)
        except Exception as e:  # 任何一帧处理出错都不能让子进程退出——写 stderr，继续读下一帧
            sys.stderr.write(f"[feirou_tools] 处理帧出错：{e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
