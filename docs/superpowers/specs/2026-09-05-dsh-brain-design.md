# 肥肉大脑（dsh_brain）设计文档

日期：2026-09-05
状态：设计已口头确认，待用户过目后拆实施计划

## 0. 一句话

把肥肉的 AI 回复从「每条消息调一次接口」改成「一个常驻的、被管控的智能体」：
大脑跑在 mac-mini 的一个容器里（DeepSeek Harness，下称 dsh，SDK 模式常驻），
有自己的沙盒工作区、记忆、技能，只能读知识库、不能碰别的机器；
回复必须经过一个「说话工具」，工具带硬约束，保证像人一样简短。

## 1. 为什么做

现状（2026-09-05 从 mac-mini 66 条问答日志 + 面板配置核实）：

- 人设、接口、知识库开关散在面板 5 个接口配置 + 3 个插件里，群里和私聊里是两个肥肉。
- 模型把人设里的示例句当口头禅逐字照抄（「我刚从键盘上趴起来」）。
- 编造：不存在的 Telegram 群链接、「每天抽 30 BTC」「累计抽了 79 BTC」。
  后者的根因是签到往来被写进对话记忆又喂回模型。
- 几乎每条回复结尾带「还要我继续吗」，两个字的消息换来一大段回复。
- 上下文条数配成 1000，坏回复被当范例复读（CLAUDE.md 3.15 已记）。
- **杭州美食群已经在生产上走 dsh**（2026-09-05 二次调研补记）：`api_configs[4]` → mac-mini `:8437`
  `hzfood_gateway.py`（源码在本机 `~/Personal/hz-food-map/deploy/dsh/`）→ 每条消息冷起一次
  `dsh --profile headless`（40–120 秒）→ 技能 `hz-food-map` + 公网 MCP `food.bigsong.site/mcp`。
  它没有会话、没有闸门、每次冷启动。**本设计上线到美食群 = 替换它，不是并存。**

这些换运行时不会自动好，所以本设计的核心不是「换成 dsh」，而是三件事：
**说话有闸门、记忆有边界、权限有围栏**。dsh 只是承载这三件事最省事的底座。

## 2. 硬约束（按用户 2026-09-05 拍板的顺序）

1. **网络：只允许我们连它，不允许它连我们。**
   大脑是 Tailscale 上一个独立节点（容器内自跑 tailscaled，打标签 `tag:feirou-brain`）。
   ACL 只放行：
   - `win-shukong → brain:8500`（网关）
   - `brain → mac-mini:8434`（NCC 知识库只读检索）
   - 其它 tailnet 方向一律拒绝。
   容器出口另加防火墙：只放 `lo`、`tailscale0` 上述一个端口、以及公网 `key.bigsong.site:443`
   （模型 API）与 `food.bigsong.site:443`（杭州美食地图 MCP，数据在 hkbohai，不在 mac-mini）。验收判据：容器内 `nc -vz 192.168.1.8 10001` 必须失败，
   `nc -vz 100.71.182.5 22` 必须失败。
2. **沙盒：它的一切操作只在自己的工作区里。** 工作区只放肥肉自己的东西（见 §4.2）。
   dsh 沙箱模式 `workspace-write`，不挂 bash / pwsh 工具。
3. **学到的东西分两级**：个人记忆它自己直接写；共享知识只能「提议」，
   进面板待审区，人通过后才落盘。
4. **签到不经过大脑。** 签到插件的钩子在 AI 之前、命中即返回（`wxbot_core.py:3936`），
   本来就不会调 AI；本设计额外保证签到往来**不喂进**大脑的会话。
5. **知识库只读。** 大脑没有 mac-mini 的文件系统，只有一个 HTTP 检索端点（8434 的 `/retrieve`）。
   共享知识的写入由面板代做，大脑本身零写权限。美食地图是例外：它的 MCP 本来就带写操作
   （收录推荐），而且真相源在 hkbohai、有自己的鉴权，沿用现状。

## 3. 总体结构

```
 微信 ──wxautox──▶ wxbot_core（win-shukong）
                     │ 四个 getter 钩子（同 ncc_kb）
                     ▼
              plugins/dsh_brain ──HTTP /reply──▶ 大脑网关（容器 :8500）
                     │ 失败/超时                    │ stdio JSON-RPC
                     ▼                              ▼
              model_fallback 退回老接口        dsh --profile sdk（常驻）
                                                    │ 工具
                                   ┌────────────────┼──────────────────┐
                                   ▼                ▼                  ▼
                              wx_reply /       kb_search /        fs 读写
                              no_reply /       hzfood(MCP) /      （仅工作区）
                              propose          web_search
                                   │
                                   └──▶ 网关拿到气泡 ──▶ 返回机器人 ──▶ 发微信
```

三个新部件：

| 部件 | 在哪 | 干什么 |
|---|---|---|
| 大脑容器 `brain/` | mac-mini（OrbStack，arm64） | dsh sdk 常驻 + 网关 + 本地 MCP 工具 + tailscaled + 出口防火墙。网关同时暴露 `/reply`（插件用）和 OpenAI 兼容的 `/v1/chat/completions`（过渡用，见 §4.5） |
| 机器人插件 `plugins/dsh_brain/` | win-shukong 本仓库 | 决定哪些会话走大脑；把消息转成网关请求；失败退回老链路 |
| 面板页 `/dsh_brain` | win-shukong `web_server.py` + 模板 | 待审区、会话开关、最近回复及其思考、技能列表、健康 |

代码位置：大脑容器的全部代码放本仓库 `brain/` 子目录（Dockerfile、网关、工具、人设、技能）。
mac-mini 上 `~/feirou-brain` 是本仓库的一个 clone，只用 `brain/`。
运行数据（记忆、会话、提议、日志）在 `~/feirou-brain-data/`，挂进容器，**不进库**。

## 4. 大脑容器

### 4.1 dsh 的用法

- 常驻 `dsh --profile sdk`，网关通过 stdio JSON-RPC 驱动（2026-09-05 已在 mac 上实测：
  initialize 0.6s，每轮 7.7–9.5s，同 session 记得上文，不同 session 隔离）。
- 一个微信会话（一个群或一个私聊对象）= 一个 dsh session，sessionId 就是会话名。
- 模型：主用 `songkey/songkey-auto`（用户 2026-09-05 指定；网关自己挑当下能用的上游）。
  已知风险：mac 上 dsh settings.yaml 的 8-27 注释记着它会被路由到 grok-4.5、长 write 参数
  会挂。大脑的工具参数都很短（气泡 ≤ 220 字），期 0 专门验它调 MCP 工具的成功率，
  低于 9/10 就退到 `deepseek-v4-flash`（已实测每轮 8–10s）。
- 人设走工作区根目录的 `AGENTS.md`（dsh 会读 cwd 下的说明文件，实测确认）。
  它读 cwd 这个特性因此是**受控的**：cwd 里只有肥肉自己的东西。
- 通过 profile patch 关掉：`dsh-tool-bash`、`dsh-tool-pwsh`、`dsh-tool-subagent`、
  `dsh-tool-web`（联网只走我们自己的 MCP）、`dsh-schedule`。
- 会话持久化：sdk profile 自带。**待验证**：容器重启后同一个 sessionId 是否接续。
  接不上就由网关用自己的回复日志重新预热（§4.5）。

### 4.2 工作区布局（容器内 `/brain`，宿主 `~/feirou-brain-data/workspace`）

```
/brain
  AGENTS.md              人设（进库，从 config/prompt/肥肉.md 改写，示例句全部删掉）
  knowledge/shared.md    共享知识（只读：属主是网关用户，dsh 用户只读）
  memory/people/<人>.md  个人记忆（dsh 可写，单文件上限 4 KB，超了网关截尾并在面板标黄）
  memory/groups/<群>.md  群记忆（同上）
  skills/<名>/SKILL.md   技能（进库；frontmatter 写 groups: [...] 或 scope: all）
  proposals/             共享知识提议（dsh 通过工具写，人工审）
  log/                   网关的回复日志（每条：输入、思考摘要、工具调用、气泡、耗时）
```

两个 Linux 用户：`dsh`（跑 dsh，只对 memory/ 和 proposals/ 有写权）、`gw`（跑网关，
拥有全部文件）。共享知识只读靠文件属主实现，不靠模型自觉。

进库的两样（`AGENTS.md`、`skills/`）源文件在仓库 `brain/workspace/` 下，容器启动时由
入口脚本复制进 `/brain`（覆盖同名，属主 `gw`，`dsh` 只读）；其余目录都是运行数据。

### 4.3 工具（全部是本地 MCP server，Python，stdio）

| 工具 | 参数 | 约束 / 行为 |
|---|---|---|
| `wx_reply` | `bubbles: list[str]` | 唯一的说话通道。群 ≤ 2 条、私聊 ≤ 3 条；总字数 ≤ 长度预算（§4.4）；违反则返回错误文本让它改，第 2 次仍违反网关直接截断。 |
| `no_reply` | `reason: str` | 不接话。取代现在的 `[NO_REPLY]` 文本标记。 |
| `propose_shared_knowledge` | `text, source` | 写 `proposals/<id>.json`，状态 pending。 |
| `kb_search` | `query` | 调 mac-mini:8434 新增的只读 `/retrieve` 端点，返回片段 + 固定事实清单。 |
| `mcp__hzfood__*` | （原样） | 不是我们写的：直接挂公网 MCP `https://food.bigsong.site/mcp`（拷 `~/.dsh/profiles/headless/cordis.patch.yml` 那段），技能 `hz-food-map` 说明怎么用。 |
| `web_search` | `query` | 复用现成 grok-search MCP（走 key.bigsong.site）。 |

大脑写在正文里的任何文字都**不会**发到微信，只是它的草稿。网关只认工具调用。

### 4.4 回复闸门（确定性，不靠模型自觉）

- **长度预算**：`budget = clamp(30 + 2.5 × len(对方消息), 40, 上限)`，群上限 150 字，
  私聊上限 220 字。字数按去空白后的字符数。参数在 `brain/gateway/config.yaml`。
- **反口头禅**：网关保留全局最近 50 条、本会话最近 20 条已发气泡。
  新气泡与其中任一条开头 8 字相同，或 4-gram Jaccard > 0.5，退回重写一次并附提示
  「换个说法开头」；再犯就照发但在日志里标记。
- **剥收尾套话**：最后一条气泡若以问号结尾且含「需要我 / 要不要 / 还要 / 想知道 /
  继续吗 / 要我」，整条剥掉；若它是唯一一条，只剥那一句。
- **剥 Markdown**：复用 `plugins/reply_shape/strip_markdown`（纯函数，不带 wxbot_core，大脑代码直接 import）。
- **预热过滤**：先过 `plugins/context_guard/guard.filter_history`（时间戳条目、兜底文案、`[NO_REPLY]`、
  "没法联网"整轮连坐，这些规则都是踩过坑的），再叠签到规则；签到触发词直接用
  `plugins/wechat_checkin/handler.TRIGGERS`，不再抄一份词表。
- 网关的这些函数都是纯函数，单独成模块 `brain/gateway/shape.py`，可在 mac 上裸跑单测。

### 4.5 网关 API（容器 :8500，仅 tailnet 可达）

- `POST /v1/chat/completions`（OpenAI 兼容，**过渡路径**）：让机器人不改一行代码就能把某个会话
  指到大脑——面板里加一个接口配置，URL 指 8500，**模型名编码会话**：`feirou:group:<群名>` 或
  `feirou:chat:<昵称>`。网关从最后一条 user 消息解析发言人（机器人的群消息格式是 `昵称: 内容`），
  从 messages 里的历史做一次预热，回复用 `||SPLIT||` 拼气泡。杭州美食群第一个走这条路
  （它现在就是这么接 8437 的，切个 URL 即可）。机器人侧的分条模板与 `reply_shape.merge_thin_parts`
  仍会对这条路径的输出再跑一遍：上限（群 6 条 150 字）比大脑的宽，只会把碎片合并、不会撑破。
- `POST /reply`
  请求：`{conversation, is_group, sender, text, image_b64?, prime?: [{sender, text, time}]}`
  响应：`{bubbles: [...]}` 或 `{no_reply: true, reason}` 或 HTTP 5xx。
  `prime` 只在该会话首次出现时由机器人带上（最近 20 条历史，已过滤签到往来），
  网关把它作为第一条用户消息的前缀「以下是此前的聊天记录」喂进去。
  超时 60 秒（机器人侧 70 秒）。
- `GET /proposals`、`POST /proposals/<id>/approve|reject`：审核；approve 追加到 `knowledge/shared.md`。
- `GET /log?limit=50`、`GET /health`、`GET /skills`。
- 每条请求给大脑的用户消息前缀一行上下文：`[群聊:共建杭州美食地图 | 发言人:xx | 2026-09-05 14:02 | 相关技能:杭州美食]`。
  技能匹配按 SKILL.md 的 `groups` 字段。

### 4.6 故障处理

| 故障 | 处理 |
|---|---|
| dsh 进程死 | 网关守护重拉，期间 `/reply` 返回 503；机器人退回老接口 |
| 单轮超时 | 网关取消该 session 的当前 prompt，返回 504 |
| 模型不调 `wx_reply` 就结束 | 网关追加一条「请用 wx_reply 回复或 no_reply」再给一次机会，仍不调则返回 `no_reply` 并记日志 |
| 工具约束违反 | 见 §4.3、§4.4 |
| 知识库端点不通 | 工具返回「检索不可用」，大脑按不知道处理（人设里写明不许编） |
| 空消息 / 只有表情或附件 | 网关直接返回 no_reply，不进大脑（09-05 hzfood 日志：一条空输入让 dsh 把网关源码当项目写成了"开发汇报"） |
| songkey 401 | 网关启动时用 key 打一次 `/v1/models`，失败直接退出并打日志（09-05 03:00 hzfood 就静默吃过 401） |
| 记忆文件超限 | 网关截尾 + 面板标黄 |

## 5. 机器人侧插件 `plugins/dsh_brain/`

- 钩子：和 ncc_kb 一样挂 `_resolve_group_api` / `_resolve_chat_api` /
  `_get_group_prompt` / `_get_chat_prompt`，**排在 ncc_kb 前面**；命中就返回一个
  `BrainAPI(conversation, is_group)` 实例，其 `.chat(message, prompt, history, image_path, image_url)`
  签名与四个上游 API 类一致。
- 走插件路径时**绕过 `_build_split_prompt`**（大脑不需要分条提示，人设由它自己管），只保留
  `_parse_split_reply`；`ai极客-冷酷版` 那个群不进大脑，直到有一个"冷酷语气"技能能替代它。
- `.chat()`：POST `/reply`；`bubbles` 用 `||SPLIT||` 拼接返回（上游分条逻辑照旧生效，
  reply_shape 的合并照旧生效）；`no_reply` 返回 `[NO_REPLY]`（现有接话闸门照旧生效）；
  任何失败返回固定串 `API返回错误，请稍后再试`，让 `model_fallback` 沿备用链走。
- 首次见到某会话时从 `history` 取最近 20 条做 `prime`，过滤规则：内容命中
  签到触发词或形如兑换码的行。
- 配置 `plugins/dsh_brain/data/config.json`：`endpoint`、`enabled_groups`、`enabled_chats`、
  `excluded_*`、`timeout`。第一期 `enabled_groups` = 两个测试群，`enabled_chats` 由用户指定。
  改配置下一条消息生效，不用重启。
- 单测 `tests/test_dsh_brain.py`：mock HTTP，覆盖拼接、no_reply、失败退回、prime 过滤。

## 6. 面板页 `/dsh_brain`

薄路由三条（页 / state / action），逻辑在插件 `panel.py`，不 import flask、不 import wxbot_core
（沿用 ncc_community 的做法）。页面内容：待审提议（通过 / 驳回）、开了大脑的会话列表（增删）、
最近 50 条回复及其思考摘要、技能列表、网关健康。面板到网关的调用走 tailnet（ACL 已放行）。
单测 `tests/test_dsh_brain_panel.py`。

## 7. 知识库侧改动（mac-mini `~/ncc-kb`，不在本仓库）

`ncc_rag_proxy.py` 加一个只读端点 `POST /retrieve`：入参 `{query}`，出参
`{context, is_ncc, facts, meta}`，直接包现成的 `retrieve()` + `load_facts()`。
不改 `/v1/chat/completions`，老链路不受影响。改前照规矩备份 `.bak-<日期>`。

杭州美食地图那边**不用改**：MCP 直接挂；技能文件的真相源是本机 `~/Personal/hz-food-map/deploy/dsh/SKILL.md`，
拷进 `brain/workspace/skills/` 后两边要同步改。大脑接管美食群之后，`launchctl bootout` 掉
`com.hzfood.gateway`，并改 `~/Personal/hz-food-map/README.md` 第 25–28 行的链路说明。

## 8. 模拟与上线判据

`brain/sim/replay.py`：把真实对话按顺序喂给网关（sim 模式：照常跑闸门，不发微信）。
数据源：mac-mini `~/ncc-kb/logs/qa-*.jsonl`（66 条）+ 机器人 `memory/<wxid>/<会话>/*_memory.json`
（55 个会话，选 5–8 个有代表性的）。输出一份 Markdown 对照表：原回复 / 新回复 / 思考摘要 / 耗时 /
触发了哪些闸门。可选 `sim/judge.py` 用 songkey 模型按「简短 / 像人 / 不重复 / 不编造」四轴打分做参考。

上线到测试群的判据：
- 100 条回放里，零条超预算、零条重复开头、零条收尾套话；
- 用户看过对照表认可；
- songkey-auto 跑通一轮；若期 0 判定它工具调用不稳，改用 deepseek-v4-flash 再跑一轮。
之后由用户在测试群真机测试，再逐步扩到其它会话。

## 9. 分期

| 期 | 内容 | 产出 |
|---|---|---|
| 0 验证 | sdk session 重启接续？AGENTS.md 人设生效？patch 关工具生效？songkey-auto 调 MCP 工具稳不稳（10 次里成功几次）？OrbStack 容器里跑 tailscaled + 出口防火墙可行？ | 五条各一句结论 |

期 0 结论（2026-09-05）：① session 重启不接续，`RESULT: RESUME_LOST`（第二个进程问"我叫什么"答案为空、不含"松爸"）——`Gateway._prime_if_new` 是必须项，不是兜底。② patch 生效：`--dump-config` 显示 `id: tool-bash` 命中 `disabled: true`，人设文本原样出现在渲染结果里（`PATCH persona present: True`），实际调用 `列出全部工具名` 时返回的工具表里不含 bash/pwsh 等 shell 工具，只有 MCP 工具（`mcp__feirou__echo`、`mcp__hzfood__*`）和只读/规划类内置工具。③ `songkey-auto` 调 MCP 工具成功率 `RESULT: model=songkey-auto tool_calls_ok=10/10`，选定 `songkey-auto` 为主用模型，不需要再测 `deepseek-v4-flash` 备选。（过程中发现并修正了 brief 模板本身的一个阻断性 bug：`config: policy: never` 会让 `id: approval`/`id: permission`（`@deepseek-ai/dsh-user-approval` / `@deepseek-ai/dsh-permission-presets`）两个插件因缺少 `shell` 服务而永久 `pending`、进程直接起不来，改为对这两个 id 都 `disabled: true` 后才能正常启动；已同步进 `brain/profile/cordis.patch.yml.tmpl` 并加注释。）

| 1 大脑本体 | 网关 + 工具 + 闸门 + 人设 + 技能目录 + 回放工具，先在 mac 本机跑 | 对照表第一轮 |
| 2 容器化 | Dockerfile、tailscaled、ACL、出口防火墙、两个用户、数据卷 | 验收判据 §2.1 通过 |
| 3 机器人接入 | 插件 + 面板页 + 知识库只读端点 | 单测全绿 |
| 4 上线 | 测试群真机测 → 杭州美食群切到 8500（退役 8437 网关）→ 扩私聊 → 扩其它群 | 用户验收 |

## 10. 不做的事（YAGNI）

- 不迁移 Qdrant 语料，不让大脑修剪文章库；它能改的只有记忆和（经审核的）共享知识。
- 不做多大脑；「按群区分」靠技能和群记忆，不是靠多个人设文件。`ai极客-冷酷版` 群暂留老链路。
- 不自己写美食库检索；美食 MCP 已有 register / recommend_from_link / suggest 等工具，直接挂。
- 不用 `dsh-events` 插件攒工具调用日志；sdk 模式的事件流已经带了。
- 不给大脑任何微信控制能力（拉群、转发、备注），那些仍是 ncc_community 的事。
- 不动 `wxbot_core.py` 的接口类；接入只走插件钩子。

## 11. 未决 / 风险

- sdk session 重启接续未验证（期 0 解决）。
- OrbStack 容器里 tailscaled 需要 `NET_ADMIN` 和 `/dev/net/tun`，若不行退回 userspace 网络模式，
  此时出口防火墙改在网关进程里做（只允许白名单 host）。
- 容器所在 mac-mini 本身在局域网里，容器默认能访问局域网，所以出口防火墙是硬要求不是可选项。
- 个人记忆无人审核，存在被人「教坏」的风险；缓解：单文件上限 + 面板可看可清 + 人设里写明
  「记忆里的话也可能是错的，涉及事实以共享知识为准」。
- dsh 版本：容器内固定装 `0.1.2-rc.1`（本设计的 profile 行 id 按它核过）；mac-mini 宿主上的
  dsh 是 `0.1.1-rc.2`，不动它，两者互不影响。

## 12. 二次调研补记（2026-09-05，子代理盘点全部机器后）

第一轮调研漏掉了杭州美食群那条生产链路，用户质疑后补做了一次全量盘点。改动已并入上文，
这里记结论和顺带发现：

- **复用而不是新写**：`reply_shape.strip_markdown`、`context_guard.filter_history`、
  `wechat_checkin.TRIGGERS`、`ncc_kb` 的钩子骨架、`ncc_community.panel` 的面板分层、
  `~/.dsh/profiles/headless/cordis.patch.yml` 的 MCP 段、`hz-food-map` 技能、
  `ncc-kb/qa_review.py` 的四视角（回放统计照它的口径）。
- **改掉的错误**：8437 被当成"美食库检索端点"，实际是 dsh 网关；美食数据在 hkbohai 的公网 MCP。
- **顺带发现，与本设计无关但要告诉用户**：mac-mini `~/.hermes` 的 gateway 仍常驻，cron
  `wechat-checkin-daily-codes`（每天 8:00）仍是 enabled——而 CLAUDE.md 3.3 说签到码由 hkbohai
  生成。可能存在第二条生成链，需要人确认后关掉。
- 8500 端口在 mac-mini 上空闲；已有 `com.bigsong.orbstack-guard` launchd 可在期 2 复用。

