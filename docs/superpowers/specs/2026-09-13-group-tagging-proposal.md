# 群🐶标签：从「被动等人说话」改成「见到群就打」—— 提案草稿

> 状态：**草稿，未拍板，未改任何代码**（2026-09-13）。
> 只读分析 + 方案；实现前先过文末 §5 的拍板问题。
> 行号以 2026-09-13 的 `main`（b57e725）为准。

## 0. 一句话结论（先说这对你意味着什么）

**现在「发现新群」这条路根本走不到真正的新群。** 肥肉被拉进一个群之后，只要这个群没被人手动写进
`config/config.json` 的 `group` 列表（也就是没开独立监听窗口），它的消息在主循环里会被上游一句
「私聊全局监听收到群聊消息，跳过」直接扔掉，插件的 `discovery` 永远看不到它。
09-11 晚上「老友记们」就是活标本：松爸先发了 `/添加群 老友记们`，15 秒后群里 @ 了一句，
才有「发现新群：老友记们」——而且当场打备注还失败了（切窗口切歪）。

产品诉求「群一出现就打上标签」要成立，**触发点必须挪到全局监听读到群消息的那一刻**
（`wxbot_core.py:5134` / `5188` 那两处「跳过」）——那一刻主窗口正好就停在这个群上，
群名、类型、有没有备注都已经在手里，打备注不用再切窗口，是整条链路里最便宜也最安全的落点。

推荐方案 A（§3.1）：在这个跳过点加 1 处最小 hook（约 6 行），逻辑全放 `plugins/ncc_community/discovery.py`，
复用 `audit.plan_remark`（未命名群/超长/已打过 的判定它已经全有）和 `remark.confirm_group_window / verify_remark`。
已被独立监听的 6 个群和管理群**排除在自动打标之外、只提醒**（原因见 §1.6，牵一发动 config/人设/大脑索引）。

---

## 1. 现有机制的真实行为（逐条给证据）

### 1.1 新群什么时候会被「发现」？—— 只有已在 `config.group` 里的群

**证实了你的怀疑。** 消息进入插件有两条路，都到不了真正的新群：

| 路 | 代码 | 能不能看到新群 |
|---|---|---|
| ① 独立子窗口监听回调 `message_handle_callback` friend 分支 | `wxbot_core.py:3478` → `plugins/ncc_community/__init__.py:28-49` → `discovery.handle_discovery` (`discovery.py:26`) | 只对 `AddListenChat` 开过窗口的会话触发。群窗口只在启动时按 `config.group` 逐个开（`wxbot_core.py:2825-2829`）或 `/添加群` 现场开（`wxbot_core.py:4560-4577`）。**不在 `config.group` 里的群没有子窗口，回调永远不来。** |
| ② 全局监听 `get_next_new_message` | `wxbot_core.py:5125-5234` | `Next_callback` 里 `if self.wx.chat_type != 'group'` 否则 `log('私聊全局监听收到群聊消息，跳过')`（`5134` / `5164`）；主体循环 `if msg.attr == 'friend' and chat_type != 'group':`（`5188`）—— **群消息整段跳过**，`5217` 那个 ncc hook 在 `5188` 的 if 里面，群消息到不了它。 |

`discovery.handle_discovery` 自己也说得很直白（`discovery.py:26-27`）：「群消息入口（在 friend 消息处理里……调用）」。
`forward.py:2089-2091` 的注释也承认：「discovery 是被动的（群里有人说话才登记），一直沉默的群从来没进过后台」——
但实际比这更窄：**不是"沉默的群"进不来，是"没被写进 config.group 的群"进不来，说多少话都没用。**

**日志实锤（`panel_logs/log_260911.txt:847-865`）**：
```
[09-11 20:44:02]: 私聊全局监听收到群聊消息，跳过            ← 老友记们里有人说话，被扔掉
[09-11 20:47:33]: … 窗口：松爸 … 消息：/添加群 老友记们        ← 人手动加进 config.group
[09-11 20:47:36]: 添加群组 老友记们 监听完成
[09-11 20:47:50]: … 窗口：老友记们 发送人：松爸 - 消息：@🐶肥肉 ping
[09-11 20:47:50]: [ncc_community] 发现新群：老友记们           ← 这才"发现"
[09-11 20:47:51]: [ncc_community] 打备注前切群没确认成功 老友记们: 当前窗口不是群聊（chat_type=friend）
```
最后一行：`apply_remark` 在**子窗口回调线程**里对**主窗口**做 `ChatWith("老友记们")`（`remark.py:123`），
主窗口当时停在「松爸」私聊上，切换没成功/没结算就读 `ChatInfo()`，判成 friend，打标失败。
`registry.json` 里「老友记们」至今 `status=pending, remark_applied=false`（本次读取 registry.json 确认）。
这正是 `forward._read_chat_name` 在 8-14 事故后加的「等结算 + 重读 3 次」（`forward.py:1059-1085`）——
`remark.apply_remark` 没用上那套，只读一次就信。

顺带：`panel_logs/log_260811.txt:524-525` 等 3 组「发现新群：野生新群 / 已打🐶备注」不是生产事件，
是 `tests/test_ncc_engine.py:598` 的用例（fixture 就叫「野生新群」）当天在项目根跑单测灌进生产日志的，
`common.py:22-27` 在 08-14 就为此加了 `NCC_LOG_SILENT`。别当成"自动打标曾经跑通过"的证据。

### 1.2 被拉进群时微信给什么消息？现有 system hook 能不能看到？

- 微信侧确实是 system 消息，`welcome.py:16-21` 的正则就是照着它写的：
  `"张三"邀请"李四"加入了群聊`、`"张三"通过扫描…二维码加入群聊`。生产日志里大量样本，例如
  `log_260912.txt`: `类型：system 属性：system 窗口：共建杭州美食地图🐶 … 消息："跳舞"邀请"祭山"加入了群聊`。
- **但这些全是"别人被拉进【已监听】群"的消息。** 肥肉自己被拉进新群那条（应为 `"xx"邀请你加入了群聊`），
  9 月所有 `panel_logs` 和抽查的 `wxauto_logs`（0908/0909/0912/0811/0814）里**一条都没有**——
  因为新群没有子窗口，system 消息和 friend 消息一样进不了 `message_handle_callback`；
  在全局监听那条路上，`5188` 只放行 `attr == 'friend'`，system 直接丢。
- `welcome.py:37-51` 的 `handle_welcome` 还有一层：只处理 `cfg["welcome"]` 里配了的群，并在 `48-51`
  专门把「新人是机器人自己」排除掉——它是为迎新写的，本来就不承担"发现自己被拉进群"。
- **推测（需实测）**：被拉进群这一刻，新群会出现在会话列表里，但"邀请你加入"这类 system 消息
  **不一定**给会话打未读红点；`GetNextNewMessage` 是按未读跳会话的（`docs/wxauto-api-reference.md:311`），
  没红点就要等群里第一个人说话才冒出来。方案 B 的成立与否全押在这一点上，见 §3.2。

### 1.3 「已登记但显示名没🐶」的提醒为什么反复来？

逻辑在 `discovery.py:36-53`：`is_known` 为真 → `not audit.has_dog(who)` → `_SEEN` 去重 → `notify_admin(...)`。
反复触发的三个原因叠在一起：

1. **`_SEEN` 是进程内存**（`discovery.py:19`），每次重启清零。9 月重启次数（`grep -c '监听器初始化完成'`）：
   09-02 ×1、09-07 ×1、**09-08 ×5**、09-09 ×1、09-12 ×1、09-13 ×1。每次重启后该群第一条 friend 消息就提醒一次。
2. **两个群天然就"没🐶"却又在登记表里**（registry.json 实读）：
   - `NCC 社群管理肥肉售后维权🤖`：`remark_applied=false`，名字以🤖结尾。它是真正的管理群，但 `store` 里
     `admin_group` 配的是「爱和一切肥肉测试群」，所以 `discovery.py:33` 那句 `who == cfg.get("admin_group")`
     **排除不了它**，它的每条 friend 消息都走进 discovery。（`forward._admin_group_names` 在 `forward.py:2079-2083`
     倒是把 `store.DEFAULT_CONFIG` 里的默认管理群也算进去了，但 discovery 没用那个函数。）
   - `老友记们`：`status=pending, remark_applied=false`（1.1 那次打标失败留下的）。
3. `admin_group` 配的是测试群，提醒全发到「爱和一切肥肉测试群」。

**实锤（`wxauto_logs/app_20260912.log:494-503`）**：17:45 重启后，17:59:04 老友记们第一条消息 →
`切换聊天窗口: 爱和一切肥肉测试群` → `发送消息: 🤖 群「老友记们」在登记表里，但微信显示名没有🐶标签（备注多半没打上）。`
注意这次 `SendMsg(who=admin)` 是**在监听线程里直接切主窗口**（`common.py:91`），没拿 `MAIN_WINDOW_LOCK`——
跟 09-11 的打标失败是同一类"从子窗口回调里操作主窗口"的动作。

### 1.4 `GetAllRecentGroups()` 能不能当主动扫描的数据源？

能用但只能做兜底，不适合做"立刻"：

- 实测返回 `List[Tuple[str,int]]` =（显示名，成员数），`panel_logs/log_260811.txt` 两次「扫群」：
  `共 104 项 … ('NCC的朋友们16群🐶', 216) … ('爱和一切肥肉测试群', 2)`，耗时 **49.6 s / 24.5 s**，
  且两次前 5 项不同（会话列表滚动位置不同），佐证 CLAUDE.md 3.6「非全量、每轮 95–102 个不等」。
- 显示名截断：`('游牧岛｜游牧护照持有者（会员🐶', 352)` —— 这条是 `remark_overrides` 里人为定的短备注
  （`store` config.json 实读），不算截断证据；截断本身以 CLAUDE.md 3.6 的实测为准（约 16 字）。
  `forward._fix_remarks` 因此每个群都要 `ChatWith` 过去再 `ChatInfo()` 读完整名（`forward.py:2135-2148`）。
- 它要**滑一遍会话列表**，全程持 `MAIN_WINDOW_LOCK`（`forward.py:2115-2116`），期间主循环的
  `GetNextNewMessage` 轮询停摆——半分钟到一分钟没人收私聊。
- `ChatInfo()` 只有 `{'chat_type','chat_name','group_member_count'}`，`chat_name` 是**显示名**
  （有备注就是备注），读不到真名（`remark.py:96-100`、`audit.py:125-135`、`docs/wxauto-api-reference.md:180`）。
  但反过来这正是判"打没打过"的唯一客观依据：**显示名带🐶 = 打过了**（`audit.plan_remark`）。
- 另有一条没人用过的信息：`GetNextNewMessage` 返回值里**有备注时会多一个 `remark` 键**
  （`docs/wxauto-api-reference.md:127`，文档示例）。方案 A 里可以顺手记下来做交叉验证，但别依赖（未实测）。

### 1.5 「群还没改名」怎么判定？

- **样本证实**：微信未命名群的显示名 = 成员昵称用「、」拼接。registry.json 里有两条：
  `松爸、Outsider大曹、睿南、晓、一味照烧饼、AI德华、灵翘Lynn、刘鑫睿Rio、Veronique、Susan Liu、吴航`（11 人）和
  `松爸、王伟`（2 人）——都是 Notion 时代同步进来的，`remark_applied=true`（Notion 标题带🐶推断的假绿，PANEL_SPEC §1 #7），
  `allow_forward=false`、无分组，就是"没人管的未命名群"。
- 现有代码已经在用这条判据：`audit.plan_remark` `audit.py:172-175`
  `if "、" in name and name not in known: return FIX_SKIP, "没有群名的群（微信显示成员名「…」）…请人来定"`。
  `and name not in known` 这个例外是对的：万一哪个真群名里带顿号，登记过就不会被误判。
  全表 117 个群里带「、」的只有上面两个，误判面很小。
- 成员数（`group_member_count`）**不是**可靠信号：`松爸、王伟` 2 人是未命名群，`爱和一切肥肉测试群` 2 人是命名群；
  名字长度也不是（未命名群随人数从 5 字到 40 字都有）。
- 「改名后何时补打」：显示名一变，下一条消息在全局监听里冒出来的就是**新名字**，对登记表来说它就是一个
  从没见过的群 → 走新群流程打标。前提是**未命名时不要把「、」名登记进去**（否则留一条永远对不上的死条目，
  跟 §1.5 那两条一样）。只在内存里记"这个「、」名今天看过、别每条消息都判一次"即可。

### 1.6 打完🐶之后要同步什么？（决定自动打标的边界）

- **只转发、不监听的群（100+ 个 NCC 群）**：登记表 `mark_remark_applied` 落盘即可（`registry.py:411-418`），
  寻址串自动切到备注（`registry.target` `146-149`、`match_key` `231-238`）。**不用碰 config.json。**
- **在 `config.group` 里独立监听的群**（现有 6 个：`NCC 社群管理肥肉售后维权🤖 / 🏜️AI 及其代理人联邦🐶 / 共建杭州美食地图🐶 / 📈🐶 / 肥肉测试1🐶 / 老友记们`，config.json 实读）：
  打完备注显示名变了，**下次重启** `AddListenChat(旧名)` 找不到 → 消息只剩全局监听 → 被 `5134` 跳过 → 群整个聋掉
  （CLAUDE.md 3.6「2026-09-06 美食群断了 1 小时」）。要同步 `group` / `group_api_map` / `group_prompt_map`
  三处 key，还有 mac-mini 上 `brain/workspace/skills/index.json`（精确匹配群名）和 `memory/<wxid>/<群名>/`。
  `WXBotConfig.add_group/remove_group`（`wxbot_core.py:874-892`）能写盘，但改完还得防面板"旧表单整份写回"（同上那条坑）。
  **这不是插件能独立、原子地做完的事**，所以推荐"监听中的群不自动打，只提醒"。
- 管理群**绝不能打**：`forward._admin_group_names` 的注释（`forward.py:2080-2081`）——打了备注 `chat.who` 变成「群名🐶」，
  指令入口就关掉了。这是 `_fix_remarks` 和「查新群」都排除管理群的原因（`forward.py:2132`、`1564`）。

### 1.7 主窗口占用与并发（新逻辑要放在哪个线程）

- `MAIN_WINDOW_LOCK` = `wxlock.WX_LOCK`（RLock，`wxlock.py:29`；`forward.py:44` 起别名）。转发时另举 `set_forwarding` 闸门：
  主循环整轮让路（`wxbot_core.py:5384-5386`），监听回调入口 `wait_while_forwarding`（`3411`），
  dsh_brain 的 worker 也等它（`plugins/dsh_brain/dispatch.py:132`）。
- **全局监听 `get_next_new_message` 跑在主循环线程**，它本身就是"主窗口的主人"：`GetNextNewMessage` 刚把主窗口点到
  某个群上并读完消息，函数返回时主窗口还停在那个群。**在这个点打备注 = 零切换、零搜索、零 ChatWith 静默失败风险。**
  与之相比 `discovery.apply_remark` 从监听线程去 `ChatWith` 是逆着来的（1.1 的失败就是这么来的）。
- dsh_brain 并发回复（`async_reply=true, max_workers=3`，`plugins/dsh_brain/data/config.json` 实读）只在**子窗口**上
  `SendMsg`，不碰主窗口（CLAUDE.md 3.21「跨会话发送不需要切群」）；wxautox 内部 `ui_transaction` 会把并发发送串行化。
  所以新逻辑在主循环线程里持 `MAIN_WINDOW_LOCK` 做 2–3 秒的 `SetGroupRemark`，对大脑回复无影响；
  对私聊轮询的影响就是这 2–3 秒，一个群一辈子只发生一次。
- `notify_admin`（`common.py:79-93`）是主窗口操作但不拿锁，任何线程都可能调——这是现存隐患，新方案里要么在锁内调、
  要么改走飞书 webhook（记忆里有约定：状态类告警只发飞书；「发现新群」算产品通知还是状态告警，见 §5 Q3）。

---

## 2. 用户诉求 → 规则的翻译

| 用户原话 | 规则 |
|---|---|
| 「看到群出现就要立刻打上标签并同步」 | 触发点 = 群**第一次**在全局监听里冒头（不等人手动加监听）；打完当场写登记表 |
| 「群还没改群名（只有几个人名的群）就不用处理」 | 显示名含「、」且不在登记表 → 跳过，**不登记**，内存里记一下别重复判 |
| 「已经被改成其他群名、被拉进这类群时第一时间打」 | 显示名不含「、」、不含🐶、≤48 字节 → 当场 `SetGroupRemark(名+🐶)` + 回读复核 + `add_pending` + `mark_remark_applied` + 提醒去面板归类 |
| （隐含）改名后 | 新名字下一条消息冒出来即按新群处理；旧「、」名从没登记过，无残留 |

---

## 3. 方案

### 3.1 方案 A（推荐）：全局监听跳过点打标 —— "第一条消息即打"

**触发时机**：`get_next_new_message` 读到 `chat_type == 'group'` 的那一批消息时（`wxbot_core.py:5167-5168` 之后、
`5188` 之前），不管消息 attr 是 friend 还是 system（所以 §3.2 那条"邀请你加入"若真能冒头，这里免费顺带覆盖）。

**hook（`wxbot_core.py` 唯一一处，约 6 行）**：
```python
# ncc_community plugin hook: 全局监听读到群消息 → 见群打🐶（逻辑在 plugins/ncc_community/discovery.py）
if chat_type == 'group':
    try:
        from plugins.ncc_community.discovery import handle_global_group
        handle_global_group(self, chat, messages_new, msgs)
    except Exception as _ncc_err:
        log(level="ERROR", message=f"ncc_community plugin error: {_ncc_err}")
```
放在黑名单过滤（`5172`）之后、`if msgs:` 之前；不改 `5134`/`5188` 那两句上游代码（合并冲突面最小）。

**判定规则（全在插件里，纯函数复用 `audit.plan_remark`）**：
1. `name = messages_new['chat_name']`；`admin` 群、`bot.config.group` 里的群 → **不打**，
   若不在登记表就 `add_pending` + 提醒"这是监听中的群，请人工走「修备注」并同步 config"（§1.6）。
2. `audit.plan_remark(name, known, overrides)`：
   - `FIX_OK`（已带🐶）→ 不在登记表就 `add_pending` 并直接 `mark_remark_applied`（说明是「修备注」打过但没登记的），
     在登记表就 `touch_last_seen`。**不发"已登记但没🐶"提醒**（那条提醒改成 §3.4 的日报）。
   - `FIX_SKIP` 且原因是「、」未命名 → 内存 `_UNNAMED_SEEN` 记一下，静默。
   - `FIX_SKIP`（超 48 字节）/ `FIX_CONFLICT`（追加垃圾）→ `add_pending`（不打）+ 提醒一次（落盘去重，见 4）。
   - `FIX_APPLY` → 进入 3。
3. **打标**（主循环线程，`with MAIN_WINDOW_LOCK`，总预算 ≤ 5 s）：
   `remark.confirm_group_window(wx, name, expect)`（显示名严格相等 + chat_type=group + 无别的备注，`remark.py:42-86`）
   → `SetGroupRemark` → `sleep 1` → `remark.verify_remark`（`remark.py:89-110`）→ `registry.add_pending` + `mark_remark_applied`
   → `notify_admin("发现新群「X」，已打🐶，去面板归类：<url>")`。
   这一段就是 `forward._do_set_remark`（`forward.py:2183-2202`），可直接抽出来复用，不新写 UI 原语。
4. **失败/重试**：复核不过 → `add_pending`（`remark_applied=false`）+ 条目上记 `tag_attempts`、`tag_last_error`；
   该群下一条消息再试，**最多 3 次**，第 3 次失败提醒人工。去重靠登记表字段而不是内存 `_SEEN`（重启不再翻车）。
5. **同步**：登记表当场写；config.json **不动**（自动打标已排除监听中的群）。
6. **对现有指令的取舍**：
   - 「查新群」（`forward._find_unmarked`）：保留，降级为"兜底体检"；A 上线后它应该长期报「✅ 没有漏网的」。
   - 「修备注 全部」/「检查群组」：原样保留（未命名群改名前、超长群名、追加垃圾这三类仍要人来定）。
   - `discovery.handle_discovery`（friend 分支那条）：保留 `touch_last_seen`，**去掉自动 `apply_remark`**（它只对
     config.group 里的群触发，而这些群按 §1.6 不该自动打）和"已登记没🐶"即时提醒（改日报）。
7. **风险与回滚**：
   - 插件 `data/config.json` 加 `discovery.auto_tag_global: false` 默认关；hook 本身 try/except；回滚 = 关开关或删那 6 行。
   - 误打风险：`confirm_group_window` 是 2026-08-03 事故后的严格版，且这次**不需要 ChatWith**，切歪的路径根本不存在；
     剩下的只有"显示名带顿号的真群名"这一种误判，且被 `name not in known` 兜住一半（先登记就不会被判未命名）。
   - `filter_mute=True`（config 实读）：被人设成免打扰的群不会在全局监听冒头 → 打不到。新群默认不免打扰，可接受；
     被静音的老群靠「修备注」兜底。
   - 主窗口占用：每个新群一次、2–3 秒，在主循环线程内，与转发闸门天然互斥。

**为什么推荐它**：它是唯一同时满足「立刻」「不用人先 /添加群」「不靠 ChatWith 搜索」「不改上游跳过逻辑」的落点；
新增代码集中在插件内、复用三段已在生产验证过的原语；hook 1 处。

### 3.2 方案 B：入群瞬间打标 —— 抓「"xx"邀请你加入了群聊」系统消息

**触发时机**：肥肉被拉进群那一刻的 system 消息。
**成立前提（未证实，推测）**：这条 system 消息会给会话打未读、从而被 `GetNextNewMessage` 捞出来。
9 月日志里没有任何一次这类消息出现在全局监听路径上（§1.2）——可能是从没被拉进过新群，也可能是它压根不打红点。
**实现**：和 A 完全相同的 hook 与判定，只是多认一个 pattern（`welcome.py:16-21` 的正则改成认「邀请你」）。
**结论**：B 不是独立方案，是 A 的一个"更早的触发样本"，成本为零、收益要实测。建议实现 A 后做一次实验：
拉肥肉进一个已命名的测试群，不说话，看 `panel_logs` 里 `get_next_new_message` 有没有冒出该群；
冒出来 = 入群瞬间就打上；没冒出来 = 等第一条消息（A 的语义），差的是"第一个人说话前"这段时间。
**不建议**为了"入群瞬间"另起 `GetSession()` 轮询会话列表（`docs/wxauto-api-reference.md:72`）：每几秒滚一次列表和
`GetNextNewMessage` 抢主窗口，收益只是提前几分钟。

### 3.3 方案 C：定时主动扫描（`GetAllRecentGroups` → 逐群核对）

就是现在的「修备注 全部」按时跑。**零新代码**：`task_runner`（`plugins/ncc_community/task_runner.py:59-62`，每 10 秒消费
`data/task_request.txt`）已经能在 bot 进程内执行「修备注 预览 / 全部」，mac-mini 写一行文件即可触发。
**代价**：一次 25–50 s 滑列表 + 每个未打群 ChatWith（3–5 s），持 `MAIN_WINDOW_LOCK` 期间私聊轮询停摆；只覆盖"最近"约 100 个会话；
显示名截断要逐个切进去读；不满足「立刻」。**定位**：A 的每周兜底（挑 01–07 点跑，CLAUDE.md 3.18 的零失败时段），
不是主路径。**不推荐单独做**。

### 3.4 顺手要修的两件事（不论选哪个方案）

1. 「已登记但没🐶」提醒（`discovery.py:44-52`）改成**每天一次的汇总**（或并进「查新群」的报告），去重落盘，
   不再每次重启刷一条；`admin` 判定改用 `forward._admin_group_names`（把默认管理群也排掉）。
2. `remark.apply_remark`（`remark.py:113-148`）若还保留给批量/指令用，`ChatWith` 后要用 `forward._switched` +
   `_read_chat_name` 那套"等结算 + 重读"，否则 1.1 那种 `chat_type=friend` 的假失败会一直有。

---

## 4. 方案对比

| | A 首条群消息即打（推荐） | B 入群系统消息 | C 定时扫描 |
|---|---|---|---|
| 触发 | 群第一次在全局监听冒头 | 被拉进群瞬间（**前提未证实**） | 每天/每周 |
| 用不用 ChatWith | 不用（主窗口已在该群） | 不用 | 每群一次 |
| hook | wxbot_core 1 处 6 行 | 同 A | 0（现成指令 + task_runner） |
| 覆盖 | 所有会说话的非免打扰群 | 取决于红点 | "最近" ~100 个 |
| 主窗口占用 | 每新群 2–3 s 一次 | 同 A | 每轮 1–8 分钟 |
| 未命名群 | 跳过不登记，改名后当新群 | 同 A | `plan_remark` 已跳过 |
| 监听中的群 / 管理群 | 不打只提醒 | 同 A | `_fix_remarks` 已排除管理群，**不排除监听中的群**（要补） |
| 回滚 | 关开关 | 同 A | 不跑就是了 |

---

## 5. 实现前要你拍板的问题（多选题）

**Q1. 未命名群（显示名是「松爸、王伟」这种）怎么处理？**
- (a) 完全不登记、不打；改名后当新群处理 ← 推荐（对应你说的"不用处理"）
- (b) 登记为 pending 但不打，面板里能看到"有个没名字的群"
- (c) 也打🐶（不推荐：名字随成员变，备注会变成过期的人名串）

**Q2. 已在 `config.group` 独立监听的群（现在 6 个，含老友记们）和管理群，自动打标吗？**
- (a) 不自动打，只提醒；人工「修备注」后手改 config 三处 + brain 索引 ← 推荐（§1.6 的连锁改动插件做不完整）
- (b) 自动打，并由插件改 `config.json` 的 `group/group_api_map/group_prompt_map`（要重启生效；brain 索引仍要人改；面板旧表单会盖回去）
- (c) 老友记们这一个现在就人工打上，以后新群一律先走 A 再决定要不要加监听

**Q3. 「发现新群 / 打标失败」的通知发哪？**
- (a) 管理群（现状；注意现在 `admin_group` 配的是「爱和一切肥肉测试群」）
- (b) 飞书 webhook（记忆约定：状态类告警只走飞书）
- (c) 发现新群→管理群，失败/需人工→飞书

**Q4. 「已登记但没🐶」这条提醒？**
- (a) 改成每天一次汇总 ← 推荐
- (b) 直接删掉，靠「查新群」按需看
- (c) 保持现状（每次重启刷一次）

**Q5. 要不要先做那个实验（拉肥肉进一个命名的测试群、不说话，看全局监听冒不冒头）？**
- (a) 要，实现 A 之前先测，顺便确认 `GetNextNewMessage` 返回里有没有 `remark` 键 ← 推荐（半小时，机器人不用停）
- (b) 不测，直接按"第一条消息"语义上线
- (c) 顺便也测一下 `filter_mute=True` 下新群默认是不是非免打扰

---

## 6. 附：本次读过的证据清单

- 代码：`wxbot_core.py` 2276-2295（`MainWindowChat`）、2759-2935（`init_wx_listeners`）、3400-3520（`message_handle_callback`）、
  3556-3640（`process_message` 开头）、4560-4577（`/添加群`）、5059-5240（`ALLListen_mode` / `get_next_new_message`）、
  5365-5430（主循环闸门）；`plugins/ncc_community/` 的 `discovery.py`、`remark.py`、`audit.py`、`batch.py`、`welcome.py`、
  `common.py`、`wxlock.py`、`registry.py`（146-149、231-272、371-430、492-514、605-640）、
  `forward.py`（115-135、1055-1160、1530-1600、2079-2202）、`task_runner.py`、`panel.py`；`plugins/dsh_brain/dispatch.py` 90-175；
  `tests/test_ncc_engine.py` 596-645；`docs/wxauto-api-reference.md` 60-146、180、300-320；`PANEL_SPEC.md` 全文。
- 数据：`plugins/ncc_community/data/registry.json`（117 群：active 101 / unreachable 15 / pending 1；无🐶 40；`remark_applied=false` 4）、
  `data/config.json`（admin_group / welcome / remark_overrides）、`config/config.json`（只读了 `AllListen_switch / group / group_api_map / group_prompt_map / AllListen_filter_mute / group_switch` 几个键）、
  `plugins/dsh_brain/data/config.json`。
- 日志：`panel_logs/log_260811.txt`（扫群返回结构、单测灌日志）、`log_260911.txt:842-870`（老友记们全过程）、`log_260912.txt`、`log_260913.txt:176-216`（今天重启）、
  `wxauto_logs/app_20260912.log:494-503`（重启后提醒实锤）、`app_20260908.log:19993-19995`、`app_20260909.log` 抽查。

---

## 7. 拍板结果与实施计划（2026-09-13 用户拍板：1a / 2b / 3b / 4a / 5a，按方案 A 做）

### 7.1 决策
- **Q1=a**：未命名群（显示名含「、」且不在登记表）不登记、不打；内存记一下当天别重复判；改名后当新群。
- **Q2=b**：监听中的群（`config.group` 里的，含老友记们）**也自动打**，打完由插件同步 `config/config.json`
  的 `group` / `group_api_map` / `group_prompt_map`，并把 `memory/<wxid>/<旧名>/` 复制成 `<新名>/`；
  brain 的 `workspace/skills/index.json` 插件不动，飞书通知里提醒人去改。管理群（`forward._admin_group_names`）仍然绝不打。
  老友记们：部署后立刻打上（走 7.3 第 4 步）。
- **Q3=b**：发现新群 / 打标成功 / 打标失败 / 需人工，一律发飞书 webhook（`webhook_send.send_message`），不发管理群。
- **Q4=a**：「已登记但没🐶」改成每天一次汇总（飞书），去重落盘。
- **Q5=a**：先上线**观察模式**（开关默认关：只记日志、不打备注），用户拉肥肉进一个已命名测试群、不说话，
  看全局监听冒不冒头、`GetNextNewMessage` 返回里有没有 `remark` 键，再打开开关。

### 7.2 组件（全部在 `plugins/ncc_community/`，wxbot_core 只加 1 处 hook）
| 单元 | 职责 | 依赖 |
|---|---|---|
| `wxbot_core.get_next_new_message` hook（约 6 行） | 全局监听读到 `chat_type=='group'` 的一批消息时调 `discovery.handle_global_group(bot, chat_name, messages_new, msgs)`；try/except 兜住 | — |
| `discovery.handle_global_group` | 观察日志 → 判定（`audit.plan_remark`）→ 打标（复用 `forward._do_set_remark` 抽出的原语，**不 ChatWith**）→ `post_tag` | audit / remark / registry / tagsync |
| `discovery.handle_discovery`（friend 分支，监听中的群） | 保留 `touch_last_seen`；**去掉**从监听线程 `apply_remark` 的动作和即时提醒；改为：若显示名没🐶且允许自动打 → 往 `task_runner` 队列写一行「修备注 <群名>」，让它在 bot 进程后台线程持闸门去做（那条路 ChatWith+确认+回读都是生产验证过的） | task_runner / forward |
| `tagsync.post_tag(bot, old_name, new_name, source)` | 打标成功后的统一收尾：`registry.add_pending`/`mark_remark_applied`；若 `old_name in bot.config.group`：改 config.json（`group` 列表替换；两个 map **旧键保留、加新键**，值相同——运行中 `chat.who` 可能仍是旧名）、备份 `config.json.bak-<日期>`、复制 memory 目录；飞书通知（含「面板先点加载配置再保存」「brain index.json 两个名字都列上」） | registry / WXBotConfig / webhook_send |
| `discovery.daily_untagged_digest` | 每天一次（schedule，默认 09:00）：登记表里 `remark_applied=false` 或显示名没🐶的群汇总一条飞书；`data/discovery_state.json` 记上次发送日期 | registry / webhook_send |
| 插件 `data/config.json` 新段 `discovery` | `auto_tag_global`(默认 false=观察模式) / `auto_tag_listened`(默认 false) / `digest_time`(默认 "09:00") / `max_attempts`(3)；每次调用读，改了立即生效 | store |

### 7.3 实施步骤（TDD，mac 上单测纯 mock）
1. `tagsync.py` + 单测：post_tag 的登记表写入、config 三处同步（用临时 config.json）、memory 目录复制、通知文案；`old_name` 不在 `group` 时不碰 config。
2. `discovery.handle_global_group` + 单测：观察模式只记日志；未命名跳过不登记；已带🐶只 touch；超长/追加垃圾 → pending + 飞书一次；
   `FIX_APPLY` → 打标原语被调用、复核不过则 `tag_attempts+1`、第 3 次失败飞书叫人；管理群绝不打。
3. `handle_discovery` 改造 + 日报 + 单测；`test_ncc_community.py` / `test_ncc_engine.py` 里依赖旧行为的用例改掉。
4. 部署：清 `__pycache__` → `schtasks /run /tn SWXPanelRestart` → 面板启动机器人 → 观察模式跑实验（5a）→ 打开
   `auto_tag_global` → 打开 `auto_tag_listened` 并往 `task_request.txt` 写「修备注 老友记们」→ 核对 config.json 三处 + 飞书通知。
5. CLAUDE.md 3.6 补一段；提交。

### 7.4 不做的
- 不轮询 `GetSession()` 抢「入群瞬间」；不改上游 `5134/5188` 两句跳过；不动 brain index.json；不做 C（定时扫描）——
  「查新群」「修备注 全部」原样保留当兜底。
