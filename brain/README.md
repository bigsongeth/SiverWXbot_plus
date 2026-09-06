# 肥肉大脑（`brain/`）

微信机器人「肥肉」的常驻智能体：一个网关进程包着一个常驻的 dsh（`@deepseek-ai/dsh`，sdk 模式），
模型只能通过 MCP 工具「说话」，说什么、说多长、说几条都由网关的确定性闸门把关。
设计文档：`docs/superpowers/specs/2026-09-05-dsh-brain-design.md`；本期（期 1）计划：
`docs/superpowers/plans/2026-09-05-dsh-brain-phase1.md`。用户拍板的硬约束见设计文档 §2，改代码前先读。

容器化（期 2）还没做；网关目前由这台 mac 的 launchd `com.bigsong.feirou-brain` 常驻
（`~/Personal/feirou-brain/run_gateway.sh`，绑 100.127.39.63:8500，模型 deepseek-v4-flash，数据目录 `~/feirou-brain-data`）。
机器人侧插件 `plugins/dsh_brain/`（CLAUDE.md 3.21）**2026-09-06 起全量接管**：所有群聊与私聊（除文件传输助手）的 AI 回复
都交给网关；网关不通时自动退回老接口链。各群原来的人设变成风格技能：`ai-geek-cold` 挂「🏜️AI 及其代理人联邦」，
`crypto-cynic` 备用（目前没有币圈群在监听），其余群和私聊用 PERSONA 本体（融合版肥肉）。

## 跑起来

```bash
export SONGKEY_API_KEY=...            # 只进环境变量，别写进任何文件
FEIROU_DATA=~/feirou-brain-data python3 brain/run.py
```

```bash
curl -s -X POST http://127.0.0.1:8500/reply -H 'Content-Type: application/json' \
  -d '{"conversation":"肥肉测试1🐶","is_group":true,"sender":"松爸","text":"黑多岛现在还能去吗"}'
```

- `run.py` 启动时先拿 key 打一次 `key.bigsong.site/v1/models`，key 不对直接退出（09-05 hzfood 网关静默吃过 401）。
- 可选环境变量 `NODE` / `DSH_BIN`（默认 nvm 的 node 24 与全局 dsh 0.1.2-rc.1）。
- 网关按需拉起 dsh：第一条消息才起进程，initialize 约 0.6 秒；每轮 8–115 秒（songkey-auto，含工具调用）。
- 配置默认值在 `gateway/config.py` 的 `DEFAULTS`，`$FEIROU_DATA/config.json` 同名键覆盖（`port`、`model`、
  预算参数、`kb_url`、超时等）。

## 目录

| 目录 | 干什么 |
|------|--------|
| `gateway/` | 网关：`server.py`（HTTP + 一条消息一轮的编排）、`dsh_client.py`（stdio JSON-RPC）、`shape.py`/`validate.py`（预算与回复闸门）、`context.py`（用户消息前缀、预热过滤、技能匹配）、`replies.py`（口头禅记录）、`proposals.py`（共享知识待审区）、`memory_guard.py`（记忆文件上限）、`kb.py`（只读检索）、`compat.py`（OpenAI 兼容过渡路由解析）、`profile.py`（渲染 dsh patch / settings） |
| `mcp/` | `feirou_tools.py` 哑桥：wx_reply / no_reply / propose_shared_knowledge / kb_search 四个工具全转发到网关 `/tool/<name>`；`grok_search/server.js` 是 vendored 的搜索 MCP |
| `profile/` | dsh 的 `cordis.patch.yml` 与 `settings.yaml` 模板（关 shell/approval/permission 插件、挂 MCP、注入人设） |
| `workspace/` | 工作区种子：`PERSONA.md`（人设，真相源在这）、`skills/`（`index.json` 决定哪个群挂哪个技能）、`knowledge/shared.md` |
| `sim/` | 回放：把真实聊天流水打到网关出对照表 |
| `verify/` | 期 0 验证脚本与 `/retrieve` 检查脚本 |

运行数据全在 `$FEIROU_DATA`（默认 `~/feirou-brain-data`，不进库）：
`workspace/`（PERSONA 与 skills 每次启动被种子覆盖；`memory/`、`proposals/`、`knowledge/shared.md` 是运行中长出来的，不覆盖）、
`dsh-home/`（专用 DSH_HOME，别指到 `~/.dsh`）、`log/replies-YYYYMMDD.jsonl` + `log/dsh.err`、`cordis.patch.yml`（含 key，0600）。

## 网关 API（`:8500`，默认只绑 `127.0.0.1`）

`/tool/*` 与 `/proposals/<id>/approve` 都没有鉴权，只靠 `turn_id` 和本机可达性挡，所以期 1 默认只绑 127.0.0.1
（MCP 桥、回放、面板过渡路由都在本机）。期 2 容器化后再在 `config.json` 里把 `bind` 放开到 tailnet。

| 路由 | 用途 |
|------|------|
| `POST /reply` | `{conversation,is_group,sender,text,prime?}` → `{"bubbles":[...]}` 或 `{"no_reply":true,"reason":...}`。reason 有 `empty`（空文本/占位符直接拦）、`model_silent`、`timeout`、`dsh_error`。**出错时是 200 + `{"error": "..."}`**（dsh 起不来、conversation 缺失），只有网关正忙才是 503 `busy`；调用方两种都要判 |
| `POST /v1/chat/completions` | OpenAI 兼容过渡路由，模型名 `feirou:group:<群名>` / `feirou:chat:<昵称>` 编码会话；历史做一次预热；全链失败返回与 `model_fallback` 同一串错误文案让机器人走备用链 |
| `POST /tool/<name>` | 只给 MCP 桥用。说话类工具校验 `turn_id`，不是当前轮次的一律拒 |
| `GET /proposals`、`POST /proposals/<id>/approve|reject` | 共享知识待审区（期 3 面板页接这里） |
| `GET /log?limit=N`、`GET /health`、`GET /skills` | 排障 |

## 每轮怎么走

1. 网关拿全局锁（同一时刻只处理一条消息），空文本直接 `empty`。
2. 会话第一次出现时把机器人传来的 `prime` 历史过一遍 `plugins.context_guard` 的 `filter_history`（加不看插件开关的兜底）和签到过滤，作为预热喂给 dsh。
3. 用户消息首行是 `[群聊:X | 发言人:Y | 时间 | 相关技能:a,b|无 | 轮次:<turn_id>]`，dsh sessionId 是 `<会话名>#<dsh 进程纪元>`。
4. 模型调 `wx_reply` → 网关按预算 `clamp(30+2.5×len, 40, 群150/私聊220)`、条数（群≤2/私聊≤3）、反口头禅（开头 8 字或 4-gram Jaccard>0.5，含同一回复内互查）、剥收尾套话、剥 Markdown 校验；不过就退回让模型重写，第 2 次起截断放行。
5. 模型一轮结束没说话且没报错 → 追问一次（nudge）；仍沉默记 `model_silent`。超时则 `dsh.stop()` 重建进程（换纪元）。
6. 每轮落一行 JSON 到 `log/replies-*.jsonl`：turn_id、session_id、tools、attempts、reasoning、draft、耗时。

## 期 0 结论（2026-09-05）

1. **dsh session 不能跨进程接续**，而且**同名会话重启后直接不可用**：磁盘上已有同名持久化日志时，
   `session/prompt` 以 `turn/end reason=error`（`id collision`）结束、模型零输出。所以 sessionId 带进程纪元，
   连续性只靠预热；网关把这类错误如实记 `dsh_error`，不混进 `model_silent`。
2. **patch 生效**：`tool-bash` 等 shell 工具被关，模型看到的只有 MCP 工具和只读/规划类内置工具；人设原样注入。
   `approval` / `permission` 两个插件必须整体 `disabled: true`（`policy: never` 会因缺 shell 服务永久 pending 起不来）。
   write/edit 文件工具可用，但模型会乱传 `sandbox_permissions`/`justification` 导致失败，人设里已禁止。
3. **`songkey-auto` 调 MCP 工具 10/10 成功**，选定为主用模型。

## 冒烟记录（Task 8 / 9，2026-09-05）

- 「滴滴」→ 1 条 40 字内；「大理」→ 调 `kb_search`；`memory/people/松爸.md` 真写出。
- 「黑多岛现在还能去吗」（`/retrieve` 上线后）→ `tools=[kb_search, wx_reply, wx_reply]`，首条超预算被退回、
  第二条 34 字接受，115 秒：`黑多岛目前在运营，但入住要先确认房型价格。加村长/小助手问实时情况。`
- mac-mini `~/ncc-kb/ncc_rag_proxy.py` 新增只读 `POST /retrieve`（备份 `.bak-20260905-retrieve`），
  检查脚本 `sh brain/verify/kb_retrieve_check.sh`。命中 NCC 时它会打一次 rerank（外部模型，约 6 秒），只读但不是零模型调用。

## 回放（2026-09-05，`brain/sim/`）

同一批 36 条真实流水（松爸私聊 15、King 私聊 1、「AI 及其代理人联邦」群 10、测试群 2、qa 日志 8），
逐条打网关出对照表：`~/sim/replay-round1.md`、`~/sim/replay-round2-A-songkey-auto.md`、`~/sim/replay-round2-B-dsf.md`
（含真实聊天内容，不进库）。

| 轮 | 模型 | 有效回复 | 超时 | 模型沉默/误判不接话 | 合理不接话 | 平均耗时 |
|---|---|---|---|---|---|---|
| 1（60s 超时、kb 被代理吃掉） | songkey-auto(grok-4.6) | 11 | 16 | 3 | 6 | 62.6s |
| 2A（120s、kb 正常） | songkey-auto(grok-4.6) | 11 | 1 | 21 | 3 | 77.8s |
| 2B（120s、kb 正常） | deepseek-v4-flash | 30 | 2 | 0 | 4 | 48.3s |

- 第一轮所有 NCC 事实题都是在知识库不可达的状态下答的（见「已知限制」里的代理事故），不能作数。
- ⚠️ **回放会真的调 MCP，包括有写副作用的美食地图**：第二轮 B 组把测试群里那条「中国凉都黄牛肉馆 ¥93/人」
  真收进了 food.bigsong.site 的库（hkbohai `places` 表 rowid 最新一条，2026-09-05 23:00）。回放数据里凡是含高德链接、
  店名推荐的消息都会落到生产地图上；要么先在数据源里过滤掉，要么接受它。grok-search 只读，kb_search 只读。
- 美食 MCP 的工具在**所有**会话里都可见（技能索引只决定给不给它那份 SKILL.md 说明），不限于「共建杭州美食地图」群。
- 美食技能有「地图没有 → 小红书」路径（2026-09-06 加）：`suggest` 返回空就调 `mcp__grok__cn_search`，答的时候说明来源是小红书、
  末尾邀请发高德链接收进地图；小红书搜到的店绝不写进地图。实测「余杭万达有哪些好吃的」→ suggest 空 → cn_search →
  "蛙喔牛蛙(4楼)、裴社长山葵烤肉…觉得好发我高德链接"，110 秒。`~/Personal/hz-food-map/deploy/dsh/SKILL.md` 那份（老 8437 网关用）
  没同步这条，因为那条链路上没有 grok MCP；期 4 切换时以 `brain/` 这份为准。
- 美食技能有「按店名收录」路径（2026-09-06 加，替代只认高德链接）：群友明确要收录 + 店名带地点 + 有推荐语，
  `search_place(店名+地点)` → 校验城市是杭州、地标出现在返回地址里 → `recommend_place`；任一条件缺就追问，不猜不写。
  只给店名不带地点高德会匹配到外地店（实测「裴社长山葵烤肉」命中扬州店），所以地点是硬条件。
  实测「把裴社长山葵烤肉收进地图」→ 追问在杭州哪；「把余杭万达的蛙喔牛蛙记一下」→ 追问推荐语；两条都没写库。
- 推荐每家带高德短链（2026-09-06 加）：美食 MCP 新增 `https://food.bigsong.site/p/<poiid>` → 302 到 `wb.amap.com/?p=…`（手机点开跳高德定位到店），
  `suggest` / `list_places` / 收录回复里的 `amap` 都改成这个 38 字符短链；网关 `shape.text_len` 不把链接算进字数预算。
  问「地图在哪看」给公开列表短链 `https://surl.amap.com/4bvd5QZ4HY`。改动在 `~/Personal/hz-food-map`（hkbohai 已部署，
  备份 `hzfood/*.bak-20260906-shortlink`，那个仓库里的改动还没 commit）。
- 2A 里 grok-4.6 的典型失败：把回复写在正文里不调 wx_reply，被网关追问后又把追问当成"系统提示不该回"而调 no_reply，
  一条私聊连着 6 条这样丢掉；答出来的也偏客服腔、爱编细节（"社区厨房齐全"）。
- 2B 里 deepseek-v4-flash 基本按规矩走：据点/主理人/签到都答对（大曹、大理+黑多岛、"发【签到】两个字"），
  短句、有性格、附和类消息主动闭嘴；两次超时都在「搜推特」类需要 grok-search 的题上。
  "GPT-6 Astra"之类新闻来自搜索工具，本轮没有逐条核实。
- 形状闸门三项（超预算/重复开头/收尾套话）两轮几乎全 0，老链路那侧几乎每条都超预算、带"需要我…吗"。
- 模型选择是用户的决定（拍板的是 `songkey-auto`），这里只记数据；配置默认值没改。

## 已知限制（期 1）

- 全局串行：同一时刻只处理一条消息，一条慢（最长 120 秒超时 + nudge）会拖住后面的。
- **硬约束 ③ 的"共享知识只能提议"在期 1 只靠人设**：dsh 的文件沙箱是 workspace-write，`knowledge/shared.md` 就在工作区里，
  模型物理上能用 write/edit 改它（人设已明令禁止，`run.py` 也不覆盖该文件所以改了会留下）。文件属主级的只读要等期 2 两个用户落地。
- 网关没有鉴权（见上面 API 一节），默认只绑本机；改成 0.0.0.0 之前要先有 tailnet ACL。
- 没容器化、没网络隔离：只能在可信机器上跑、只接测试群。硬约束 ①（Tailscale 单向）在期 2 落地。
  dsh 会扫 `~/.agents/skills`，宿主上几十个与肥肉无关的技能曾出现在它的目录里；现在 `run.py` 把 dsh 的 `HOME`
  指到数据目录、`$DSH_HOME/skills` 软链到工作区 `skills/`（dsh 不扫工作区里的 `skills/`），实测目录里只剩
  hz-food-map / ncc-community。硬约束 ②（只看自己的工作区）真正兜底仍要等容器化。
- **sdk 运行时只有 initialize / session/prompt / shutdown 三个方法，没有取消轮次的手段**（09-05 实测
  `session/cancel` / `session/control` 都回 unknown method）。所以一轮超时只能重建整个进程：冷启动 + 重预热，
  且后台那一轮还在烧模型。超时阈值必须放在模型真实延迟之上，否则会连锁（回放第一轮一条私聊 15 轮打掉 11 轮）。
- `songkey-auto` 当前（09-05）落到 `grok-4.6`：一句「你好」也先烧 700 多个推理 token、24 秒起步，且不回传推理内容
  （日志里 reasoning 为空是它的行为，不是收集漏了）。`deepseek-v4-flash` 同题 6 秒并回传推理。模型由用户定，
  这里只记事实；回放第二轮做了两者的 A/B。
- 每轮真调模型 10–115 秒，群里体验偏慢；提速方向是模型路由与去掉 kb 侧 rerank（约 6 秒）。
- 回放对照表里的"原回复"来自老链路流水，群里老机器人没答的轮次没有对照。

## 让某个群走大脑（现在是全量：`enabled_groups`/`enabled_chats` 都是 `*`）

1. mac 上把网关常驻起来，`~/feirou-brain-data/config.json` 写 `{"bind": "<本机 Tailscale 地址>", "model": "deepseek-v4-flash"}`
   （`bind` 默认只绑本机，要让 win-shukong 打进来必须改；网关无鉴权，只准 tailnet 可达）。
2. win-shukong 上 `plugins/dsh_brain/data/config.json`：`{"enabled": true, "gateway_url": "http://<mac Tailscale 地址>:8500",
   "enabled_groups": ["肥肉测试1🐶"]}`；改配置下一条消息生效，插件代码首次上线要整进程重启一次。
3. **不要**拿面板的「OpenAI 兼容接口」配置项去指大脑：`OpenAIAPI` 是 30 秒超时 + 重试 2 次，会把同一条消息发三遍还全丢。
   `/v1/chat/completions` 兼容路由只留给没法装插件的调用方。
4. 美食群切换时再做：把该群加进 `enabled_groups`，观察一天，`ssh mac-mini launchctl bootout gui/501/com.hzfood.gateway` 退役 8437。
2. 观察一天没问题后 `ssh mac-mini launchctl bootout gui/501/com.hzfood.gateway`（退役 8437 网关）。
3. 改 `~/Personal/hz-food-map/README.md` 里的链路说明；美食技能以后只改 `brain/workspace/skills/hz-food-map/SKILL.md`，
   并同步回 `~/Personal/hz-food-map/deploy/dsh/SKILL.md`。

## 测试

```bash
for f in tests/test_brain_*.py; do echo "== $f"; PYTHONPATH=. python3 -W error::ResourceWarning "$f" 2>&1 | grep -E '^(Ran|OK|FAILED)'; done
```

`tests/fake_dsh.py` 是假 dsh（stdio 协议同款，`TURNERR` 触发错误轮次），`test_brain_dsh_client.py` 靠它跑真子进程。
直接跑文件，别用 `-m unittest`（mac 上会被 anaconda 的 tests 包遮蔽）。
