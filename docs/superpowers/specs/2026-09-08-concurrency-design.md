# 并发回复设计（2026-09-08）

> 目标：多个会话的 AI 回复可以同时进行，一条慢不再把所有人堵住。
> 前置调研与实测见本文件 §1；改造分两侧，网关先行。

## 0. 用户拍板

- **并发回复**：群里多人同时 @，并发处理、谁先好谁先发。**不做「群内串行、跨群并发」**
  （2026-09-08 明确否掉了这个更保守的方案，别在后续会话里重新提议）。
- 引用（`msg.quote`）保持现状：能引用就引用，失效自动降级成 `SendMsg(at=)`，不预先放弃。

## 1. 现状与实测（改之前的事实）

### 1.1 四道串行闸

| # | 位置 | 机制 |
|---|---|---|
| 1 | `wxbot_core.py` `WxParam.LISTENER_EXCUTOR_WORKERS = 1` | 所有被监听会话的回调跑在同一个线程 |
| 2 | `wxbot_core.py` `process_message` 群聊分支 / `wx_send_ai` | `api.chat(...)` 阻塞，回复发完才返回 |
| 3 | `brain/gateway/server.py` `Gateway.lock` | 全局一把锁，整轮持有；`self.inflight` 单槽 |
| 4 | `brain/gateway/server.py` `self.dsh` | 单个常驻 dsh 进程 |

真实代价（mac-mini `replies-*.jsonl` 最近 3 天 60 轮）：**中位 57.4s、p90 177s、最长 940s**。
群里连着两个 @，第二个人平均等一分钟才「开始被处理」。

阻塞期间**不丢消息**：wxautox 的 MessageMonitor 靠锚点快照比对找新消息，消息留在窗口里。

### 1.2 微信发送侧不需要「跳群」

`AddListenChat` 给每个会话开独立子窗口，回调给的 `chat` 就是那个窗口，`chat.SendMsg()` 直接打进去
（`docs/wxauto-api-reference.md:182`：`who`/`exact` 在子窗口上无效）。所以跨会话发送不用 `ChatWith`、
不碰主窗口，天然避开 ChatWith 静默失败那一堆坑。只有 `MainWindowChat` 回落通道和全局模式新私聊要走主窗口。

**线程安全已验**（在 win-shukong 上读包 + inspect）：`wxautox4/utils/lock.py` 的 `ui_transaction` 是
**进程内 RLock + 会话级命名互斥体**（跨进程也串行），`SendMsg` / `ChatInfo` / `SendFiles` / `quote`
都被它包着（`hasattr(__wrapped__)` 为 True，`GetNewMessage` 没包）。

推论：**并发发送安全、会排队，但永远不会真并行**；并发收益全在「等大脑那 8–115 秒可以重叠」。
⚠️ `ui_transaction` 默认 **30 秒**拿不到锁就抛 `TimeoutError` —— 有别的线程长时间占 UI（ncc 群发）时
并发发送会开始抛，worker 必须接住。

### 1.3 dsh 支持并发多 session —— 已实测

脚本 `brain/verify/concurrent_sessions.py`（用生产 patch/dsh-home/workspace，`FEIROU_GW` 指向脚本自起的
stub，不打生产网关——因为 `tool_call` 对不带 turn_id 的 `wx_reply` 会放行，有把测试话发进真实群的风险）。

判据事先定死，2026-09-08 全过。决定性证据是工具调用时间线：

```
=== 串行基线（对照组）===          === 两个 session 同时 prompt ===
+  0.0s kb_search 打点A            + 75.2s kb_search 打点B   ← B 开工
+ 17.8s wx_reply  serA             + 77.2s kb_search 打点A   ← A 开工（B 未收尾）
+ 36.8s kb_search 打点B            + 84.0s wx_reply  conB
+ 44.2s wx_reply  serB             + 85.1s wx_reply  conA
```

并发组**交错**（干活区间重叠 6.8s），串行组不交错。墙钟 69.2s → 27.4s。
另外：两轮回答零串台；`wx_reply` 的 turn_id **0 次漏带**（说明按 turn_id 归属可行）；
`kb_search` **不带 turn_id**（并发下归属不了，见 §2.2）。

**这次没覆盖**：只测 2 路；只测不写文件的轻量轮次，**多 session 同时写 `workspace/memory/` 的竞争没测**
（并发化最可能出事的地方）；跑的是 `songkey-auto`，生产已是 `deepseek-v4-flash`。

### 1.4 引用的保质期

`quote(text, at=None, timeout=3)` 是对着消息 UI 控件操作的，会过期。生产日志唯一一次失败
（`panel_logs/log_260906.txt`）：13:16:43 收到消息 → DusAPI 504 重试链 → 13:21:58 才回复，
隔 **5 分 15 秒**，报「消息对象已失效」，自动降级成普通发送，消息没丢。
平时中位 57 秒，从没失效过。**并发会缩短这个延迟，所以引用风险是下降的。**
精确阈值（时间 vs 被新消息刷走）未测。

## 2. 网关侧改造（先做）

### 2.1 `inflight` 单槽 → 按 turn_id 多槽

`Optional[Inflight]` → `dict[turn_id, Inflight]` + 守卫锁。`tool_call` 按 `args["turn_id"]` 取回自己那轮。

### 2.2 turn_id 归属规则（兼容 + 防串台）

不能一刀切要求带 turn_id：现有单测和降级路径都不带。规则定为：

- 带了 turn_id：必须精确命中在飞的某一轮，否则 `STALE_TURN` 拒绝。
- **没带 turn_id：只有在飞恰好 1 轮时回落到那一轮；在飞 ≥2 轮一律拒绝**（并发下无法归属，
  放行就是串台的正门）。
- `kb_search` 同样按这套归属（它现在连 turn_id 字段都没有 → 给 `feirou_tools.py` 的
  inputSchema 补上，人设不用改，模型看到字段自然会带）。

### 2.3 全局锁 → per-conversation 锁 + 并发信号量

- 同一会话一把锁：保序，且 `primed` / `seen` / `RecentReplies` 状态不打架。
- 外层信号量限总并发，新增配置 `max_concurrent`（默认 **3**：songkey 配额、mac-mini 内存、
  美食群那种要串一堆 MCP 工具的轮次都吃资源）。
- 拿不到 → 仍返回 `{"error": "busy"}`（HTTP 503），机器人侧照旧重试。
- `self.primed` / `self.seen` 是裸 set/dict，读改写加锁。

### 2.4 超时不再全局重建 dsh

`_restart_dsh_after_timeout()` 杀整个进程，并发下会把别人正在跑的轮次一起干掉，而 sdk 没有
`session/cancel`（2026-09-05 实测，09-08 复核仍然没有）。改为：

- 超时只把那个 turn_id 从在飞集合摘掉 → 迟到回调自然被 `STALE_TURN` 拒（这道防线实测可靠）。
- 只有 dsh 进程真的死了（`alive()` 为 False），或**连续** `restart_after_timeouts`（默认 3）轮超时，
  才重建；重建时若还有别的轮次在飞，等它们结束/超时再动手。

## 3. 机器人侧改造（后做）

不做这层，网关并发了也没人同时来问（监听线程一次只处理一条）。

1. 命中大脑分支不再阻塞：`(chat, message, 上下文)` 交给 worker 池，监听线程立刻返回继续轮询。
2. 保序：同一会话串行（并发回复是跨会话的，同一会话内仍按到达顺序发）。
3. `MemoryManager._get_lock`（`wxbot_core.py:983`）有 check-then-set 竞态，并发前补守卫锁。
4. 发送 worker 接住 `ui_transaction` 的 30 秒 `TimeoutError`，重试。
5. 发送前 `wait_while_forwarding()`，别跟 ncc 群发抢窗口。
6. `quote` 保留（§0），失效走既有降级。

## 4. 上线顺序与回滚

网关先上是安全的：**网关并发了而机器人还串行，行为跟现在一模一样**；反过来则请求全堆在网关锁上排队，
不坏但没收益。两侧都由配置开关控制，`max_concurrent=1` 即退回今天的串行行为。

---

## 5. 实现记录（2026-09-08 已落地）

### 5.1 网关侧 `brain/gateway/`

| 改了什么 | 在哪 |
|---|---|
| `inflight` 单槽 → `{turn_id: Inflight}` + `_inflight_guard` | `server.py` `Gateway.__init__` / `handle_reply` |
| 工具回调归属 `_resolve_inflight(name, args)`；带 turn_id 必须命中，不带则「在飞 1 轮才回落、≥2 轮拒」 | `server.py` `tool_call` |
| 新拒绝文案 `AMBIGUOUS_TURN`（告诉模型带上轮次重来） | `server.py` 模块常量 |
| 全局锁 → `_slots`(信号量) + `_conv_lock(conv)`；**先抢名额再拿会话锁**（反了会攥着名额空等） | `server.py` `handle_reply` |
| `_ensure_dsh` 加 `_dsh_guard`，并发下不会各起一个进程 | `server.py` |
| 超时不再立刻杀进程：`_timeout_streak` 攒够 `restart_after_timeouts` 或进程真死才重建，且**别人在飞时让路** | `server.py` `_restart_dsh_after_timeout` |
| `/health` 增 `inflight` / `max_concurrent`，`busy` 改为「名额是否用尽」 | `server.py` `state()` |
| `memory_guard.enforce` 加模块级锁（读全文→截断→覆写不是原子的） | `memory_guard.py` |
| `kb_search` inputSchema 补 `turn_id` | `brain/mcp/feirou_tools.py` |
| 新配置 `max_concurrent`(3) / `restart_after_timeouts`(3) | `config.py` |

单测 `tests/test_brain_server.py` 30 个，其中 `ConcurrentGatewayTest` 7 个用 `BlockingDsh`
（Event 精确控制「进了 prompt」「允许返回」两个时刻，不靠 sleep 撞时序）覆盖：
两会话同时在飞 / 同一会话串行 / 名额满返回 busy / `max_concurrent=1` 退回串行 /
**多轮在飞时不带 turn_id 必须拒** / 陈旧 turn_id 落不到别人槽里 / **一轮超时不连坐杀别人**。

### 5.2 机器人侧

- 新模块 `plugins/dsh_brain/dispatch.py`：每会话一个队列 + 至多一个 worker（保序），
  全局 `max_workers` 封顶。★ **worker 里回调的是 `WXBot.process_message` 本身**
  （`_local.in_worker` 防二次派发），关键词回复/图片识别/历史/分条/接话闸门/故障转移一行没重写。
- hook 1 处：`wxbot_core.py` `process_message` 开头（`attach_quote_text` 之后）。
- `MemoryManager._get_lock` 补 `_locks_guard`：原来是裸 check-then-set，两个线程同时给同一个
  **新**会话建锁会各拿一把，等于那一刻没有互斥。
- 新配置 `async_reply`(默认 **False**) / `max_workers`(3) / `queue_max_per_conv`(5)。
- 单测 `tests/test_dsh_brain_dispatch.py` 12 个。

## 6. 已知残留 / 没做的（别当万事大吉）

1. ★ **多 session 同时写 `workspace/memory/` 的竞争没测**。`memory_guard.enforce` 加了锁，
   但 **dsh 自己的 write/edit 工具没有任何互斥** —— 两个会话同时让模型改同一个记忆文件会互相覆盖。
   并发验证跑的是不写文件的轻量轮次，这条路径是空白。放量前值得专门测一次。
2. `_ensure_dsh` 重建时 `primed.clear()` / `seen.clear()` 清的是**所有**会话的状态，
   并发下别的会话会重复预热一次（多带一遍历史，不是正确性问题）。
3. `msg_replied_count` / `msg_received_count` 是裸 `+=`，并发下可能少计。只影响面板统计。
4. 顺序变化：`_handle_custom_forward`（`wxbot_core.py:3484`）现在在**派发之后立刻**执行，
   不再等 AI 回复完成。两者本就独立（注释写着「不影响原有流程」），但顺序确实变了。
5. 失败告警降级：同步路径下 `process_message` 返回 falsy 会走 `is_err`（发 webhook + 管理群），
   异步后那条路径拿到的是「已派发」的 True。现在只在 `dispatch._drain` 里 log ERROR 留痕，
   **没有升级成告警**。要不要接回 `is_err` 待定（担心并发下刷屏）。
6. `dispatch.stats()` 没有面板出口，只能靠日志。
7. 并发验证只测了 2 路，3 路及以上没测；生产 `max_concurrent` 默认给的就是 3。

## 7. 上线开关速查

| 想要 | 怎么做 |
|---|---|
| 完全回到改造前 | 机器人 `async_reply: false`（默认就是）；网关 `max_concurrent: 1` |
| 只开网关并发 | 网关默认已是 3；机器人不开 async_reply → 实际仍是一次一条（安全的中间态） |
| 真正并发 | 机器人 `async_reply: true`，`max_workers` 与网关 `max_concurrent` 保持一致 |
