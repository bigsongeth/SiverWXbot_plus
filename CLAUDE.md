# CLAUDE.md — SiverWXbot_plus 项目开发指南

> 给在本项目干活的 AI 助手（Claude Code 等）看的承接文档。
> 配套文件：`AI_COLLABORATION_GUIDE.md`（DusAPI 定制、API 兼容原则、合并冲突清单）——两者一起读。
> 本文件偏"怎么干活 / 有哪些坑 / 哪些定制不能丢"，那份偏"DusAPI 定制点的逐项检查清单"。

---

## 0. 一句话背景

SiverWXbot_plus 是一个 **微信机器人 + Flask 网页控制面板**，跑在这台 Windows 主机
`win-shukong`（局域网 `192.168.1.8`，2026-07-30 前是 `192.168.3.21`；Tailscale `100.73.185.46`，用户 `admin`）上，项目根目录：

```
C:\Users\Admin\SiverWXbot_plus-main
```

日常开发通常由用户 mac 上的 Claude 通过 Tailscale SSH 远程操作这台机器（`ssh win-shukong`），所以本文档里有不少"SSH 远程操作 Windows"的坑——即使哪天直接在本机干活，这些定制点和流程依然适用。

我们是 fork 后长期维护：**定期同步上游更新，同时保留自己的定制**。所有改动的第一原则：**不要和上游打架**——新功能优先做成插件或最小 hook，减小每次合并的冲突面。

- 上游：`https://github.com/SiverKing/SiverWXbot_plus.git`（remote 名 `upstream`）
- 我们的 fork：`https://github.com/bigsongeth/SiverWXbot_plus.git`（remote 名 `origin`）
- 工作分支：**`main`**（2026-08-03 起统一到主干，生产工作树也检出在这个分支）

---

## 1. 环境与远程操作

### 中文乱码坑
CMD 默认 GBK 代码页，`git status` / `dir` 里的中文会显示成乱码（`chcp 65001` 也不稳）。
**要看中文输出，一律走 PowerShell**，它对 UTF-8 友好。纯结构化数据（分支名、路径、数字）用 CMD 无所谓。

### .bat 文件必须 CRLF + 纯 ASCII
在这台机器上生成 `.bat` 时：
- **必须 CRLF 换行**（LF 会让 CMD 在错误位置断句，报 `'us-main"' 不是内部或外部命令`）。用 Python `open(path, 'w', newline='\r\n')` 写。
- **只用英文**，别写中文（GBK 乱码会直接让命令失效）。
- 别用 shell `printf`/`echo` heredoc 生成含 `()` `|` 的内容，bash 会先吃掉这些字符。用 Python 写文件再 scp。

### 含中文的文件从外面传
SSH 管道里 echo/heredoc 中文容易坏。正确姿势：在本地把文件写好（UTF-8），`scp` 到 `win-shukong` 对应路径。

---

## 2. 进程管理（血泪）

### 杀进程要精准，别误杀
`taskkill /f /im python.exe /fi "COMMANDLINE eq *web_server.py*"` **不可靠**——filter 不匹配时会杀掉所有 python，包括 SSH 会话自己。

**正确姿势：按端口杀。** 面板端口在 10001–10004：
```cmd
for /f "tokens=5" %a in ('netstat -ano ^| findstr /r ":1000[1-4].*LISTENING"') do taskkill /f /pid %a
```

### 看谁在跑
```powershell
wmic process where "name='python.exe'" get ProcessId,CommandLine /format:csv
netstat -ano | findstr LISTEN | findstr :100
```

### 启动服务的坑
- `web_server.py` 是主入口，起 Flask 面板 + 自动开浏览器。
- **`webbrowser.open()` 在 SSH 会话里会卡住**（没浏览器）。Flask 其实已经正常绑定端口了，只是 SSH 命令看着像挂了。桌面双击 `.bat` 就没这问题。
- SSH 里跑 python，stdout 会被缓冲，后台运行常看不到输出。调试时重定向：`python web_server.py > panel_logs\sshtest.log 2>&1`。
- **必须在项目目录里跑**（`cd /d C:\Users\Admin\SiverWXbot_plus-main`），代码用相对路径找 `config/`、`templates/`、`panel_logs/`。
- **重启服务前先告知用户**，很多时候用户想自己手动启动（他常说"你关闭任务，我来手动启动"）。

---

## 3. 我们的定制点（改上游 / 合并上游前必看）

策略：**核心文件只加最小 hook，业务逻辑全丢进 `plugins/`**。以下每一条在合并上游后都要确认还活着。

### 3.1 面板局域网访问（`web_server.py` 的 `host='0.0.0.0'`）★ 容易被合并冲掉
我们对面板的远程访问是靠**开放局域网监听**实现的：`web_server.py` 末尾的
```python
app.run(host='0.0.0.0', port=free_port, debug=False, threaded=True)
```
这是我们的提交 `15f0361 allow panel access from LAN`（上游原来是 `127.0.0.1`）。
- 访问地址：局域网 `http://192.168.1.8:10001/`，或走 Tailscale `http://100.73.185.46:10001/`（Tailscale 地址不随局域网换段变，优先用它）。
- 上游 V4.7.27 自己也改成了 `0.0.0.0`（"优化远程访问"），目前两边一致；但**每次合并上游后必须检查这一行**，只要上游改回 `127.0.0.1` 或改成可配置项，一律以"局域网可访问"为准修回来。

### 3.2 微信签到发码插件 `plugins/wechat_checkin/`
用户微信私聊发"签到"，机器人发一个 new-api 兑换码（展示成 20–200 🪙 BTC）。

结构：
```
plugins/wechat_checkin/
  handler.py       # 触发词识别 + 私聊判断 + 组装回复
  store.py         # SQLite 码池 & 领取记录（核心事务在这）
  import_codes.py  # 从 gui-us 同步过来的 JSON 导入码池
  data/checkin.sqlite3
  imports/         # 每日 JSON 码池备份
```

`wxbot_core.py` 里只有 ~10 行 hook（约 3305 行附近，`wx_send_ai` 私聊 AI 回复之前）：
```python
# wechat_checkin plugin hook: keep business logic outside wxbot_core.py.
from plugins.wechat_checkin import handle_checkin
handled, checkin_reply = handle_checkin(chat, message)
if handled and checkin_reply:
    result = chat.SendMsg(checkin_reply)
```

关键设计（别破坏）：
- **一人一天一个**靠 SQLite 唯一约束兜底：`claim_records` 上 `unique(user_key, claim_date)`。
- 领码在 `begin immediate` 事务里做（查已领 → 取一个 unused → 标 claimed → 写记录 → commit），`IntegrityError` 时递归重试。这是防并发重复发码的命根子。
- 用户唯一标识 `build_user_key`：优先 `wxid/user_id/sender_id`，退而求其次 `sender/who/name`。**wxautox4 私聊里通常没有真 wxid**，实际落到 `who`（昵称/备注），风险是改备注会被当新用户——已知、可接受。想确认字段就发"签到调试"，handler 会回显 chat/message 的可用字段。
- 触发词：签到/打卡/领码/领取兑换码/兑换码/key（`normalize_text` 会去空格标点）。
- 日界线用 **UTC** 算 `claim_date`（`store.py` 里 `now.astimezone(timezone.utc).date()`），文案却说"早八前有效"——这是刻意的，配合 gui-us 每天北京时间 8:00 生成、次日 8:00 过期的批次。改时区逻辑前先想清楚。

### 3.3 兑换码来源（hkbohai → win-shukong，2026-07-04 重建）
码不是本机生成的。生成端在 **hkbohai**（154.221.25.64，脚本 `/root/new-api/scripts/`，线上库 `/root/key-newapi/data/one-api.db`——注意 `/root/new-api/data/one-api.db` 是陈旧副本，别用错）：
- hkbohai cron 每天北京 8:00 跑 `/root/new-api/scripts/run_daily_checkin.sh`：生成 100 个码，先写 new-api `redemptions` 表（保证可兑换），再导出 JSON 到 `/root/new-api/data/wechat_checkin_exports/` 并刷新 `latest.json`。
- 本机计划任务 `WechatCheckinPull`（每天 8:05，S4U 免存密码，Admin 身份）跑 `plugins/wechat_checkin/pull_and_import.py`：scp 拉 `latest.json` → 存 `imports/wechat-checkin-<date>-pull.json` → 导入本地 sqlite（重复导入自动去重）。日志在 `panel_logs\checkin_pull.log`。
- 免密链路：`C:\Users\Admin\.ssh\id_ed25519` → hkbohai authorized_keys，ssh config 别名 `hkbohai`。
- **hkbohai 的 sshd 会踢频繁重连**，pull 脚本内置 3 次重试；手动调试时别快速连发 SSH（复用 ControlMaster 连接可绕过）。
- 历史：生成端原在 gui-us（Oracle 云），2026-06-22 迁到 hkbohai 但调度器仍留在 gui-us 遥控；gui-us 2026-06-29 宕机导致断供，2026-07-04 重建成上述两端自治结构，不再依赖第三台机器。
- BTC → quota 换算：`quota = btc_amount * 500000`（new-api `QuotaPerUnit=500000`，`custom_currency` 展示成 🪙 BTC）。20 BTC = 10,000,000 quota，200 BTC = 100,000,000 quota。
- **不改 new-api 源码**，只用独立脚本 + 现成兑换码表。

### 3.4 Webhook 通知（`webhook_send.py` + 面板页）
仿云雨消息通知模块加的。面板里可配 URL/body 模板（`$title`/`$content`）。这是我们想提给上游的 PR 之一。有对应单测 `tests/test_webhook_send.py`。

### 3.5 DusAPI / 模型兼容定制（详见 AI_COLLABORATION_GUIDE.md 第 4 节）
简述：`wxbot_core.py` 的 `DusAPI` 类把 GPT 分支改成 `else` 兜底（支持 grok/gemini 等），加了 `reasoning_content` 兜底提取和标准 Chat Completions SSE 解析。**合并上游时这几处极易被覆盖，务必按那份文件的检查清单逐项确认。** 另外去掉 dashboard 里所有 DusAPI 广告文案（保留功能，去掉推广）。

### 3.6 NCC 社群插件 `plugins/ncc_community/`（2026-07-05 加）
管理群转发 + 分群迎新卡片 + 关键词拉群，三合一。管理群「NCC 社群管理肥肉售后维权🤖」内成员即管理员（群成员关系替代旧 wxid 白名单）。

- hook 共 4 处：`wxbot_core.py` 的 `message_handle_callback` 里 3 个（friend/self/system 三个分支各一个 `from plugins.ncc_community import ...`），另有 `get_next_new_message` 里 1 个（全局模式下新私聊的首条消息不经过 message_handle_callback，这里补私聊拉群关键词入口，2026-07-12 加）。合并上游后逐个确认还在。
- ★★ **2026-08-05 起去 Notion 化，真相源改成本地 `data/registry.json` + 面板 `/ncc_community`**（设计与实现记录：`plugins/ncc_community/PANEL_SPEC.md`）。
  群的分组/允许转发/允许发言/迎新链接、分组编号、拉群关键词、新群归类、不可达群恢复，**全部在面板上改，改完下一条微信消息即生效，不用重启**。
  - **「同步」「回写notion」两条指令已下线**（还接得住，只是回一句"已下线，去面板"）；`notion_sync.py` 留在库里但**没有任何调用点**，纯粹为 `git revert` 一步回滚。机器人侧不再有任何 Notion 网络调用。
  - 主菜单编号变了：`1 转发 / 2 面板地址 / 3 待归类 / 4 迎新拉群 / 0 退出`（老的 5 仍映射到迎新拉群，照顾手指记忆）。
  - **「设拉群/删拉群」现在直写 registry**，和面板同一张表。以前它写 `config.json` 的覆盖层，于是同一个关键词两处各有一份、面板改了被 config 盖掉。`config.json` 里的 `invite.keywords` 只作为遗留覆盖层还读（迁移脚本已并走，正常是空的）。
  - **`invite_keywords` 结构化成 `{关键词: {"group":.., "enabled":..}}`**，面板上能"停用但保留"。读侧一律走 `registry.invite_map()`（自动跳过停用项、兼容老的纯字符串格式）。
  - **`remark_applied` 的「Notion 标题带🐶」兜底删掉了**——那是假绿来源：人在 Notion 手敲一个🐶，登记表就以为微信里已打备注，寻址串用「群名🐶」而微信里根本没这备注。本地记录丢了就跑「修备注 全部」从微信侧重建。
  - **改名不再自动发现**：Notion 时代改标题即隐式迁移，现在群改名要人在面板点「改名」（换 key、保 `gid`、继承微信里那个真实备注、指向它的拉群关键词跟着迁）。隐式批量迁移出过事（幽灵群、同步悄悄复活坏群），显式更好排查。
  - 群条目多了 `gid`（内部稳定 id），接替 `notion_page_id` 认人；`notion_page_id` 保留为只读遗留字段。
  - **部署要先跑一次迁移**：`python -m plugins.ncc_community.migrate_notion_off`（补 gid、关键词升级+合并，幂等，`--dry` 可预览，自动备份 `registry.json.bak-<日期>`）。新代码**兼容未迁移的老数据**，所以迁移不是硬前提，看门狗提前自动重启也不会出事。
  - ★★ **改磁盘数据格式的上线，先问"没重启的老进程读得动吗"（2026-08-05 踩到）**：迁移把 `invite_keywords` 升级成 dict 后，**还在跑的旧进程当场 `TypeError: unhashable type: 'dict'`**（旧 `invite.py` 直接拿值当群名寻址），从迁移完到人重启之间**拉群全坏**——而"人手动重启"这段时间是不可控的。处置是迁移后把关键词临时降回纯字符串（gid 留着，旧代码不认识也不碰），等重启后再跑一次迁移。**读不动就把格式升级推迟到重启之后，别赌那段窗口没人用。**
  - **写任何面板改动都走 `registry` 的 CRUD**（`set_group_fields`/`rename_group`/`classify_pending`/`restore_reachable`/`set_grouping`/`set_invite_keyword`…），别直接改 dict 落盘——校验（分组存在、编号唯一、目标群存在）和级联（删群连带清关键词、删分组从所有群摘掉）都在那儿。
  - 面板逻辑在 `panel.py`，**刻意不 import flask、不 import wxbot_core**，所以 mac 上能裸跑单测；`web_server.py` 只有三条薄路由（页/state/action）。
- **拉群只在私聊触发**，群聊发关键词不处理（2026-07-12 定）。
- 指令表见群里发「帮助」，或 `forward.py` 的 HELP_TEXT。带空格的群名用 `|` 分隔参数。
- 运行配置在 `plugins/ncc_community/data/config.json`（分组/迎新/拉群关键词），群内指令实时写盘；**别提交该文件的运行时变更**。
- 机器人程序化回复统一带 `🤖` 前缀，指令层据此忽略自己的消息防自触发循环——别去掉这个前缀。
- **wxautox4 40.1.15 实测 `msg.forward()` 成功时返回 None**（与文档 WxResponse 不符），`forward.py` 里已按此处理，升级 wxautox 后留意这行为。
- **`ChatWith` 找不到会话是【静默失败】**：返回 falsy WxResponse、不抛异常，窗口留在原处。不接返回值就继续操作 = 在上一个窗口上干活。2026-07-29/30 拉群连挂两次就是这样：切群失败后照样调 `AddGroupMembers`，在残留的私聊窗口上点"添加成员"，报出误导人的"未选择任何新增成员"（万一选中了人还会**新建一个群**）。现在 `invite.py` / `remark.py` / `batch.py` / `forward.py` 一律"接返回值 + `ChatInfo()` 复核当前窗口"两道都过才动手，`forward._switched()` 是统一入口。**新写任何 ChatWith 调用都照这个来。**
- 拉群还有：备注名搜不到会回退用群名重试、切群重试 3 次（微信搜索结果常常不是第一时间出来）、失败按"群没找到/人没选到"分流文案、失败退配额（最多退 3 次）、当天首次失败给管理群推提醒。
- ★ **群名是寻址主键，Notion 标题里一个空格就能造出"幽灵群"（2026-08-03）**：
  有人把标题打成「⎵NCC的朋友们17群🐶」（前导一个空格），`_title()` 当时不 strip，
  同步下来就多一条 key 带空格的登记条目，跟真群**并存**——而且它也带着
  `allow_forward=True`、挂在同一个分组下，于是每次群发都多转发一次、**必然报"群不存在"**。
  现在 `_strip_dog` 首尾空白一律剥掉（🐶前后的都剥），空名拒绝回写 Notion。
  **凡是拿群名当 key / 寻址串的新代码，都要先过 `_strip_dog`。**
- ★ **群在 Notion 改名后靠 `notion_page_id` 认人，不是靠群名**：
  `upsert_from_notion` 按 page_id 找到老条目、继承它的 `remark`（**微信里真实存在的那个
  备注**）再删老 key。不这么做的话新 key 会另起一条，remark 变成「新名🐶」而微信备注
  还是「老名🐶」，寻址必然落空——"打上🐶后群名再改也锁得住"这句设计初衷，
  之前其实只在 key 不变时才成立。没打过备注的群不继承，跟新名走。
  同一个 page_id 只保留本次 Notion 认可的那个 key，过期重名残留一律清掉；
  Notion 行被删的孤儿 pid 没人认领，不受影响会保留。改名迁移会在「同步」的回复里列出来，不静默。
- **查"群发时群不存在"的三把工具**：管理群发「检查群组 全部」逐个 ChatWith 复核可达性
  （在 bot 进程内跑，105 个群要几分钟，会占用微信主窗口）；发「核对备注 全部」查备注
  有没有打错群（见下条）；离线核对则拿每个群的 `notion_page_id` 去 Notion 查当前标题，
  跟登记表 key 比对，能在不碰微信的情况下把改名/空格/孤儿全揪出来。
- ★ **备注打错群是可达性检查的盲区（2026-08-03）**：「肥肉测试1🐶」被打到了
  「泰国清迈旅居交流1群」头上，两个群还在不同分组，给测试组发消息发进了合作社群。
  **「检查群组」查不出这类错误**——错打时 ChatWith 是【成功】的（切到了被错打的那个群），
  一律报"可达 ✅"。用 `plugins/ncc_community/audit.py` + 管理群发「核对备注 全部」查：
  **拿「群名🐶」当搜索词**（微信优先匹配备注，直接把被错打的群捞出来），再读 `ChatInfo()`
  的真实群名比对。切到的群【也在登记表里】= 两个群身份串了，判错打；不在册则多半只是改名。
  **反过来按群名寻址是查不出的**——错打时登记表往往还停在 `remark_applied=False`
  （切歪了这笔没记成），按群名切到的是真群、一切正常，野备注完全隐形。
- ★★ **打备注的复核曾经放行错打，而且越重试错得越深（2026-08-03 根因）**：
  `confirm_group_window` 旧判据是 `want in (name, rmk) or want in (name.rstrip(DOG),
  rmk.rstrip(DOG))`——群名【或】备注任一匹配就放行。**这是循环论证：备注是我们自己
  打上去的，拿它确认"这是不是目标群"，等于让错误替自己背书。**
  事故链：某次切群歪了 →「肥肉测试1🐶」落在清迈群头上 → 之后再给「肥肉测试1」打备注，
  `ChatWith` 模糊搜索是**子串匹配**，命中清迈群那个错备注、切过去 → `rmk.rstrip(DOG)`
  恰好等于目标群名 → 放行 → `SetGroupRemark` **追加** →「肥肉测试1🐶肥肉测试1🐶」→
  下次重试回到第二步。而登记表那笔一直没记成（`remark_applied` 至今 False），
  **从后台完全看不出来**。
  现在：真实群名必须**严格相等**（备注只用于诊断错打、绝不用于放行）、读不到真实群名
  一律拒绝、目标群已有别的备注也拒绝。**写任何"先切窗口再改东西"的代码，
  判据只能用那个东西的【身份】（真实群名），不能用你自己将要写进去的值。**
- **打完要回读复核**（`remark.verify_remark`）：原来只看 `SetGroupRemark` 的返回值，
  而 `wxresponse_ok` 连 `None` 都判成功，等于几乎不设防。备注不可逆，登记表一旦记错
  就再也对不上微信——复核不过就不 mark，留给批量指令重来。`apply_remark` 和
  `batch._apply_one` 两条路径都接了这两道，batch 的回读在同一把锁里做。
- ⚠️ **打错的备注只能人工清**：`SetGroupRemark` 对已有备注是【追加】、空串也清不掉。
  在微信里手动清空后，把登记表该群的 `remark_applied` 复位再重打。
- ⚠️ `remark_applied=True` 有两个来源：本地真打成功过，**或 Notion 标题带🐶**
  （`upsert_from_notion` 的兜底，为的是本地 registry 重置后能恢复）。所以**人在 Notion 里
  手动敲一个🐶上去，会让登记表误以为微信已打备注**，寻址用「群名🐶」而微信里根本没这备注。
  要加🐶请走「批量打🐶」指令，别手敲。
- ★★ **`ChatInfo()` 读不到"真实群名"，也没有 remark 字段（2026-08-03 实测钉死）**：
  真机返回的就三个键 —— `{'chat_type','chat_name','group_member_count'}`。
  `chat_name` 是**当前显示名**：群一旦有备注，显示的就是备注本身
  （「NCC的朋友们16群🐶」），而不是群名。**所以微信侧根本无从知道一个群真名叫什么。**
  在此之前 `audit.classify` / `remark.confirm_group_window` 里那套"chat_name 是真名、
  remark 是备注"是上一个会话推的、从没碰过微信，全错。后果：
  ① 旧 `plan_remark` 把 97 个已打🐶的群判成"没备注"，计划打成「群名🐶🐶」——
  是「修备注 预览」挡下来的，**这类不可逆操作永远先跑预览**；
  ② `verify_remark` 只认 remark 字段，在真机上永远判失败，复核形同虚设。
  现在判据改成：**显示名带🐶=打过了，不带=没打过、此时显示名就是真名**，
  对不对拿 Notion 同步来的群名集合去核。
- ★ **微信群备注上限 48 字节（16 个汉字），超了会截断、重跑还会再追加一截**：
  给「AI+社区：我们到底需要什么样的社区🐶」（52 字节）打备注，`SetGroupRemark`
  报成功、回读还是原名（判"失败"）；重跑一次就变成
  「AI+社区：我们到底AI+社区：我们到底」——第一次其实写进去了一截。
  「游牧岛｜游牧护照持有者（会员群）🐶」同样下场。**这两个群是这次实跑打坏的，
  只能人工清空，且清完也打不上。** 已成功的里面最长的
  「黄山NCC-黑多岛《老友记》总部👯🐶」正好 48 字节。现在 `audit.REMARK_MAX_BYTES`
  卡住，超了报人工。
- **`GetAllRecentGroups()` → `List[Tuple[str, int]]` = (会话显示名, 成员数)**，
  显示名会被截断（约 16 字），完整的要切过去读 `ChatInfo`。它只覆盖"最近"会话，
  每轮扫到 95–102 个不等（会话列表滚动位置不同），**不是全量群列表**——
  所以「修备注」要多跑几轮才能覆盖到冷群。
- **「修备注 预览 / 全部」**（`forward._fix_remarks` + `audit.plan_remark`）：
  从微信侧遍历所有群，备注不是「群名🐶」就修，完事回写 Notion。
  安全性靠"期望值就地取材"——要打的备注 = 当前窗口读到的显示名 + 🐶，
  不是我们手上那个名字，所以切歪了顶多给另一个群打上它自己的正确备注。
  另有 `looks_spliced`：显示名里有一段重复出现（「【大理】春节串门一【大理】春节串」）
  就判疑似"早年不带🐶的备注被追加过"，报人工——拿 118 个真实群名兜过，零误报。
  只读诊断：「扫群」（看 GetAllRecentGroups 返回结构）、「看群 A|B」（打 ChatInfo 全字段）。
- ★ **打错的备注只能人工清，两条路都实测封死了（2026-08-03）**：
  ① `SetGroupRemark` 对已有备注是【追加】、空串也清不掉（2026-07-07 就知道）；
  ② `EditFriendInfo(remark=...)` 看着像另一条路（走 `EditRemarkWindow`，
  微信那个弹窗是预填+全选的，本该是替换语义），**但对群聊直接拒绝**：
  `{'status':'失败','message':'该方法只适用于好友页面'}`。
  ③ 自己写 UI 操作（`ChatMoreInfoWnd.get_item_control('备注')` 拿控件后 Ctrl+A 重写）
  也没走通：直接构造 `ChatMoreInfoWnd(wx.ChatBox)` 拿到的 `control` 是 None
  （面板得先点开），而"聊天信息"按钮在主窗口控件树里找不到——
  从根节点扫出来的按钮只有左侧导航那一排（微信/通讯录/收藏/…/更多/最小化）。
  摸了三轮没定位到入口，**这活不值得继续自动化**：清空后能救回来的群本来就少，
  人在微信里手点 30 秒的事，做出来的 UI 代码还脆（微信一升级就废）。
  **结论：清备注这一步交给人，清完发「修备注 全部」自动打回去。**
- ★ **改插件代码后如果行为没变，先清 `__pycache__`**：这份 checkout 走 SMB 挂载，
  从 mac 侧直接改文件时 Python 会拿旧的 `.pyc`——2026-08-03 实测：源文件已是新的
  （4 处新指令），bot 重启后仍报"不认识的指令"，`forward.cpython-312.pyc` 的时间戳
  停在改动之前。走 git merge 落盘的改动没这问题（git 会正确写 mtime），
  **直接编辑主工作树文件时记得 `Remove-Item -Recurse __pycache__` 再重启**。
- ★ **后台任务触发器 `task_runner.py`：运维指令不必真有人在管理群里发**。
  微信操作必须在 bot 进程内跑，可 bot 平时只听微信消息。现在往
  `data/task_request.txt` 写一行指令，bot 每 10 秒消费一次、后台线程执行，
  回复逐条写进 `data/task_result.txt`（`FileSink` 冒充 chat，指令层一行不改就能复用）。
  只认直接文本指令（进不了转发状态机、发不出群发消息），请求超 30 分钟当陈旧丢弃，
  执行期间举 `set_forwarding` 闸门让主循环让路。hook 在 `wxbot_core.py` 定时任务
  注册处 + 主循环 `run_pending` 的条件，共 2 处。
  ⚠️ mac 侧走 SMB 读 `task_result.txt` **有读缓存**，看到的可能是上一轮的旧内容，
  拿结果一律 `ssh win-shukong` 从 Windows 侧读。
- 单测（纯 mock 不碰微信，mac 上直接跑文件）：`tests/test_ncc_community.py`（97 个）、
  `tests/test_ncc_engine.py`（38 个）、`tests/test_ncc_batch.py`（14 个）、
  **`tests/test_ncc_panel.py`（38 个，面板 CRUD / 状态 / 操作 / 迁移脚本，2026-08-05 加）**。

### 3.7 AI 问答知识库（mac-mini，2026-07-05 加）
知识库栈在 `mac-mini:~/ncc-kb/`（Qdrant + rag_proxy，launchd 常驻），469 篇公众号文章 2175 块，
语料覆盖 2023-04 ~ 2026-05，端点 `http://100.71.182.5:8434`（Tailscale，OpenAI 兼容）。
改完 `launchctl kickstart -k gui/501/com.ncc.ragproxy`，健康检查 `curl http://100.71.182.5:8434/health`。
补语料：md 丢进 corpus/ 跑 `venv/bin/python ncc_ingest.py`。

**双模型分流**（2026-08-03 改）：闸门判完就**不要再合流到同一个模型**。
- NCC 话题 → `deepseek-v4-flash`（松 Key `key.bigsong.site`，自家免费模型）。事实由知识库供给，
  模型只管组织语言，不需要联网。实测扛得住 8700 字符知识库上下文，据点/已结束/合作站点全答对，
  而且比原来的 gpt-5.5 快一倍（总延迟 21s → 14s）。
- 非 NCC 话题 → `grok-chat-fast`（有联网搜索），**失败自动退回 `deepseek-v4-flash`**。
  grok 是外部渠道、有成本也不保稳，所以只让它接这部分流量。
- **为什么必须分流**：需要"最新消息"的提问一定落在非 NCC 那一侧。2026-08-03 私聊全开知识库后，
  松爸问"搜一下推特上 DeepSeek 的评价"被答"我没法联网"——因为整条路都合流到了没有联网能力的
  gpt-5.5 上，等于把 grok 的搜索能力废了。**改私聊/群聊的 KB 开关时记住：它换的不只是人设，
  连模型也一起换了。**
- fallback 只在"连接/状态码"层做（流式下 requests 拿到响应头就返回，此时还没吐字节，换模型是安全的）；
  一旦开始吐内容才断，就只能截断并在 qa 日志里记 error。回归：`~/ncc-kb/test_fallback.py`（4 个场景）。
- ⚠️ **grok 的联网搜索触发不稳定**：同样的问题有时搜（回答带 `[[1]](url)` 引用）有时不搜（退回训练记忆，
  实测答错过 DeepSeek 版本发布日期）。prompt 里写死"先去搜"没用，`search_parameters` 参数被中转站吃掉
  （返回 200 但不生效）。这是渠道层面的行为，值得按 zhongzhuan-ops 那套去查 `grok-chat-fast`
  上游到底挂的什么、有没有开搜索。

**话题闸门**：判定这次提问要不要挂知识库。非 NCC 提问（写代码/闲聊/时事）跳过检索，
纯人设正常问答，也更快。日志里每次都有 `[gate]` 决策行。

- **纯阈值闸门是错的，别退回去（2026-08-03 血泪）**：原设计只看向量最高分 ≥ `KB_SCORE_THRESHOLD`(0.38)。
  当初取样用的是长句问题，真实群聊全是短句，而**短 query 与长 chunk 的余弦相似度天然偏低**，
  两类分布重叠到没法用单一阈值切开——实测「主理人是谁」0.277、「崖州那边环境如何」0.203，
  比「怎么减肥」0.348、「写一封辞职信」0.382 还低。后果是最该答对的核心事实问题
  （据点/主理人/怎么加入）**全被挡在知识库外面**，模型转头拿幻觉硬答，
  实测会反问"NCC 是指哪个组织"。知识库看着健康（/health 全绿、points 正常），实则半瘫了 4 周。
- **现在是两路触发**：① `kb_keywords.txt` 关键词命中（高精度）→ 无条件走 KB，不看分数；
  ② 否则向量分 ≥ 0.32（下调过）兜底（高召回）。误召代价只是多注入一段上下文
  （人设已交代"无关就忽略"），漏召代价是编造据点和微信号——**非对称，一律偏向宽松**。
- `kb_keywords.txt` 每次请求重读，加词保存即生效，不用改代码不用重启。**关掉的据点也要留在词表里**
  （昆山/崖州），否则"三亚还能去吗"会走通用问答，读不到 facts 里"已结束"那条。
- 回归脚本思路：直接 import `ncc_rag_proxy` 调 `retrieve()`，只验闸门判定不调 LLM；
  验通用问题时把 `P.rerank` 短路掉（`lambda q,d: list(range(len(d)))`），否则每条都要打一次
  OpenRouter，30 条能跑超 2 分钟。

**固定事实清单** `mac-mini:~/ncc-kb/facts.md`，命中 NCC 话题时随上下文注入，改完即时生效。
- **facts 必须显式写"已结束"，光删不够（2026-08-03）**：三亚崖州 2026 年前已停止运营，
  但知识库里有 100+ 篇三亚文章（《三亚NCC内测招募》《登岛指南》这种召集帖）。
  把三亚从 facts 里删掉后，检索片段的份量会盖过沉默，肥肉照样热情招呼人去一个关掉的据点。
  现在 facts 有专门的「❌ 已结束」小节，`RAG_SYS` 里也写明**固定事实清单优先级高于检索片段**、
  冲突一律以清单为准。**以后关闭任何据点都照这个来。**
- 据点现状（2026-08-03 大松确认）：自营共居=大理、黄山黟县黑多岛；自营共同办公=上海虹桥
  （偏 co-working，不是共居，答"能不能住"要说清）；已结束=三亚崖州、昆山；
  合作站点（普吉岛/西安/中山/千岛湖等）非自营，走**游牧岛小程序**订住宿，
  具体清单以小程序实时列表为准，别凭文章罗列。待补：大曹那边的小程序打通内容。
- **rag_proxy 和 facts.md 不在本版本库里**（真相源在 mac-mini），改之前先
  `cp xxx xxx.bak-<日期>`。值得考虑哪天把 ncc-kb 也纳入版本控制。

**问答日志与复盘**（2026-08-03 加）：知识库要持续优化，燃料就是真实问题，
但 `ragproxy.log` 只有 `[gate]` 分数、没有问题原文，光看它什么也复盘不了。
- 现在每次请求落一行 JSON 到 `mac-mini:~/ncc-kb/logs/qa-YYYYMMDD.jsonl`：
  问题原文 / 是否挂了知识库 / 触发方式（关键词·相似度·未命中）/ 命中的词 / top 分 /
  引用了哪几篇文章 / 回答全文 / 耗时。流式与非流式都记（流式在 `gen()` 里累积 delta）。
  写盘失败一律吞掉，绝不拖垮问答链路。`QA_LOG=0` 可整个关掉。
- 复盘：`cd ~/ncc-kb && python3 qa_review.py`（可加 `--days 7` 或某天 `20260803`）。
  四个视角对应四种动作：**未命中的问题**→加关键词或补语料；**擦边球**（分数临界）→
  阈值对不对看这里；**高频问题**→值得写进 facts.md 当固定事实；**引用分布**→
  哪些语料在扛事、哪些从没被用过。
- 闸门回归脚本也放在 `~/ncc-kb/`：`regress.py`（NCC 问题，会真调 rerank，慢）、
  `regress2.py`（通用问题，短路 rerank）。改闸门后必跑。
- 微信侧另有一份聊天流水存档：`memory/<wxid>/<会话>/<会话>_memory.json`，
  **每会话上限 `memory_max_count`=3000 条，超了丢最旧的**。2026-08-03 实测 55 个会话
  共 2448 条、最大会话 1202 条，离上限还远没丢过数据。但它是流水不是问答对，
  也不知道哪条走了知识库——提取 Q&A 用上面的 qa jsonl，别用它。

**延迟**（2026-08-03 实测，问「黑多岛现在还能去吗」）：合计 21s = LLM(gpt-5.5) 13.2s
+ rerank 5.9s + embed 1.9s + qdrant 检索 0.01s，注入上下文 8703 字符。
群聊里等 20 秒体验是有问题的。想提速优先动 rerank（OpenRouter 免费模型，占 6s，
去掉或换本地重排最省事）和 LLM 选型，检索本身不是瓶颈。

### 3.9 知识库开关插件 `plugins/ncc_kb/`（2026-07-06 加）★ 让群聊/私聊可选接入知识库
不再靠 `config.json` 的 api_configs 索引挂知识库（面板保存 api_configs 会把它冲掉，踩过），改由本插件独占 KB 路由，自带端点，抗面板改动。
- **原理**：`wxbot_core.py` 四个 getter（`_get_group_api`/`_get_chat_api`/`_get_group_prompt`/`_get_chat_prompt`）各加一段最小 hook，先问插件"这会话开知识库了吗"——开了返回 KB 接口实例 + `NCC肥肉` 人设；没开走上游原逻辑。复用上游整条 AI 链路（历史/分段/图片），只换"用哪个接口+人设"。合并上游后确认这 4 处 hook 还在。
- **开=走 KB 端点+NCC肥肉人设；关=回落到该会话原本的 group_api_map/默认接口**（所以测试群已从 group_api_map 移除，靠插件路由；否则"关"会因残留 index 仍连 KB）。
- 配置 `plugins/ncc_kb/data/config.json`（endpoint / prompt_name / enabled_groups / enabled_chats
  / excluded_groups / excluded_chats），面板 `/ncc_kb` 页可视化增删，**改配置**下一条消息即生效，
  无需重启；**改插件代码**（`__init__.py`/`store.py`）要整进程重启才加载。
  **别提交该文件的运行时变更**。
- **通配 `*`**（2026-08-03 加）：`enabled_chats` 写 `"*"` = 所有私聊全开（私聊一直在新增，
  逐个列名字维护不动）；`excluded_*` 是排除名单，**优先级高于通配**，
  这样"全开但某几个不要"不用退回逐个列举。当前：群=两个测试群，私聊=`*`（排除文件传输助手）。
- ⚠️ **开 KB 会连人设一起换成 `NCC肥肉`**。私聊原本走 `default_prompt`=`AI极客`，
  私聊全开后所有私聊人设都变成肥肉。不想要就把该私聊加进 `excluded_chats`。
- **面板**：独立模板 `templates/ncc_kb.html` + `web_server.py` 三个新路由（`/ncc_kb`、`/ncc_kb/config`、`/ncc_kb/save`）+ dashboard.html 侧栏一行链接（都是新增，冲突面极小）。面板保存是"读全量 cfg 再改写"，不会抹掉它不认识的 `excluded_*`。
- 单测：`PYTHONPATH=. python3 tests/test_ncc_kb.py`（12 个，纯 mock）。私聊在全局模式也能开（补齐上游 chat_api_map 只在白名单模式生效的空缺）。

### 3.10 回复清洗与接话闸门（2026-07-08 加，改在 `wxbot_core.py`）
两个小机制，合并上游后确认还在：
- **时间戳外漏修复**：历史消息喂模型时用户侧带 `[时间] 发送者:` 前缀（刻意的，给模型时间感），但 assistant 历史必须喂纯内容，否则模型模仿格式把时间戳写进新回复。OpenAIAPI / DusAPI（Claude + OpenAI 兼容两分支）/ CozeAPI 的 `chat()` 里都有这个处理。另有兜底：模块级 `strip_leading_timestamp()` 在 `_clean_reply_for_send`（不受清洗开关控制）和 `_parse_split_reply`（逐条）剥掉回复开头的时间戳。
- **接话闸门**：人设 prompt 里约定"判断无需接话时只输出 `[NO_REPLY]`"，`wx_send_ai`（私聊）和群聊回复路径在发送前调 `apply_no_reply_gate()` 静默跳过、日志留痕。**prompt 层 opt-in**——只有 prompt 提到该标记的人设（目前 `AI极客.md`、`NCC肥肉.md` 的「接话判断」节）会触发，其他人设不受影响。标记混着正文时只发正文。
- 单测：`python tests/test_reply_gate.py`（13 个，纯函数不碰微信；mac 上 `-m unittest tests.xxx` 会被 anaconda 的 tests 包遮蔽，直接跑文件即可）。

### 3.8 远程重启与会话 2 执行器（血泪 2.0，2026-07-05）
**微信客户端进程绝对不能杀/退出**——登录必须人在屏幕点击，SSH/计划任务都替代不了。微信本地也监听 1000x 端口，**所以永远不要按端口杀进程**（2026-07-05 凌晨按端口杀把微信杀下线了）。

- 只重启机器人线程（没改核心代码）：面板 POST `/stop_bot` `/start_bot`（admin/123456）。
- 改了 `wxbot_core.py`/插件代码要整进程重启：`schtasks /run /tn SWXPanelRestart`（交互式计划任务，在用户 RDP 会话里跑 `restart_panel.bat`，只杀「python.exe 且命令行含 web_server」），之后记得面板里再启动机器人。
- 需要在会话 2（有 UI 的桌面）跑任意命令：改 `C:\Users\Admin\swx_payload.cmd` 内容 → `schtasks /run /tn SWXRun` → 输出在 `C:\Users\Admin\swx_run_out.txt`。SSH 自己在 session 0，看不到会话 2 的窗口。
- 无人值守 E2E 自测技巧：用「定时消息」让机器人自己往管理群发指令（self 消息路径与手机端操作同路径）。

### 3.11 AI 日报插件 `plugins/ai_news_note/`（2026-07-05 加，2026-07-30 才补进版本库）
每天定时把日报渲染成微信**收藏笔记**再转发到群，走的是笔记而不是长文本消息。

- 数据源：mac-mini 每天推 `C:\Users\Admin\ai_news\latest.json`，超过 `MAX_AGE_HOURS`(20h) 不发，避免发隔夜数据。
- `sender.py` 是主链路，`render.py` 渲染笔记 HTML（**微信笔记只认 `background-color`，不认文字 `color` / `<mark>`**），
  `sensitive.py` 发送前过滤敏感词。
- hook 在 `wxbot_core.py` 的 `register_daily_note`（约 2537 行）+ 定时任务分支的 `_ai_news_note_enabled`，共 2 处。
- 面板 `/ai_news` 独立页（`templates/ai_news.html` + `web_server.py` 四个路由 + dashboard 侧栏一行链接），
  开关/时间/目标群可视化改，存 `data/settings.json`，`importlib.reload(config)` 热更新；**改发送时间要重启机器人**重新注册定时任务。
- `data/`、`last_sent.txt` 是运行时数据，不进库。
- **残留编辑器窗口自锁（2026-08-01 断供两天，血泪）**：`_close_all_editors()` 原来只发
  `WM_CLOSE` 就走人、从不校验。7-31 早上 RDP 断开导致一次"桌面锁屏"失败，留下一个关不掉的
  笔记编辑器窗口，此后每次运行 `_top("Chrome_WidgetWin_0","笔记")` 都抓到这个**旧窗口**当成
  新建的笔记，往不可编辑的窗口里粘 → 校验必败 → 再留一个窗口 → 下次接着抓到它。连续 5 次
  "内容没粘进笔记编辑器（试了 3 次）"，而环境自始至终正常（同期独立进程复刻整条链路怎么测都通）。
  现已修：关不干净直接中止、点"新建笔记"前记下 hwnd 集合之后只认新窗口。
  **新写任何"点开某窗口再操作"的 UI 流程都照这个来：认新窗口，别只按 class+name 找。**
- **落点别写死像素**：老代码点正文用 `r.top + 130`，实测（编辑器 701x641）正文顶部 +60px 是
  标题行、点不进去，+89px 才可编辑 —— 而 130 正好落在 89px，只剩 29px 余量。已改为按正文
  `DocumentControl` 的实时 rect 取中心。**注意这跟屏幕分辨率是否固定无关**：编辑器是独立窗口，
  拖一下边框或微信记住新尺寸就会翻车。
- ★ **绝对不要另起进程发日报（2026-08-03 定）**：`sender.py` 开头那句"本模块在 bot 进程内、
  由 schedule 主循环单线程调用，和 bot 其它 UI 操作串行"是**硬前提，不是描述**。独立进程会和
  bot 每 3-5 秒的消息轮询抢微信主窗口 —— 实测 18:44：`18:44:57` 编辑器置前成功 →
  `18:44:58` bot 的 `更新当前聊天窗口缓存信息` 把主窗口拉回来 → `18:45:00` 粘贴时
  `点归属编辑器=False`，Ctrl+V 打在主窗口上，连败 3 次。
  外部触发改走 `trigger.py`：mac-mini 通过 SSH 写一个标记文件
  `C:\Users\Admin\ai_news\send_request.flag`，bot 在自己的 schedule 里每 10 秒消费一次
  （标记超过 10 分钟当陈旧丢弃，避免 bot 没在跑时的请求隔几小时诈尸）。
  结果仍写 `send_result.json`，格式与旧入口一致，mac-mini 那边的解析不用改。
  **计划任务 `ai_news_send_now` 保留但只作人工应急**；跳板当初要的是"进入 session 2"，
  而 bot 本来就在 session 2，写文件又不挑会话，所以它已无必要。
- **失败自动重试**（`trigger.py`，2026-08-03 加）：原来一次不成整天放弃 —— 8/3 早上
  07:04/07:05/09:30 三次全挂、当天断供，而 18:54 手动跑一次就过了，说明抢焦点故障是间歇性的。
  现在失败后隔 15 分钟重试，最多 4 次（`RETRY_DELAY_MIN` / `RETRY_MAX`）。
  重试**一律不带 force**，靠 `last_sent.txt` 防重兜底，不可能重复发群。
  重试状态在内存里，整进程重启会丢（可接受，最多少发一次重试）。
  单测：`PYTHONPATH=. python3 tests/test_ai_news_trigger.py`（21 个，纯 mock；
  测试里要先 stub 掉 `wxautox4`/`win32*` 才能在 mac 上导入本插件）。
- mac-mini 侧对应改动：`~/.hermes/scripts/ai_news_trigger_wechat.sh`（写标记而不是
  `Start-ScheduledTask`）+ routine `~/.claude/scheduled-tasks/ai-daily-digest/SKILL.md`
  第 5 步措辞。两者都留了 `.bak-20260803`。**这两个文件不在本版本库里**，改之前先备份。
- 排查工具在 `plugins/ai_news_note/diag_note_*.py`（布局/键盘/剪贴板/落点四件套）+
  `verify_note_fix.py`（只建笔记不发送的回归验证），都跑在会话 2，见各自文件头。
  另有 `win-shukong:C:\Users\Admin\ai_news\diag_fg.py`（前台焦点半程复现，只建笔记不发送，
  配 InteractiveToken 计划任务跑在会话 2）。
  两个坑写在里面了：**UIA TextPattern 读笔记正文不可靠**（内容进去了还是读回占位符 `￼`，
  判定一律以 Ctrl+A/Ctrl+C 回读剪贴板为准）；**多个落点别在同一个编辑器里连着测**
  （前一个失败会把焦点搞脏，后一个跟着败，看起来像"键盘整条不通"）。

### 3.12 AI 调用绕开系统代理（`wxbot_core.py` 模块级 `HTTP` 会话）
这台 Windows 的 IE 系统代理指向局域网某台机器的 `:7897`（换过段：`192.168.3.5` → `192.168.1.2`/mac-mini），
`requests` 在 Windows 会自动读注册表跟着走。那台机器一休眠，AI 调用全线 ProxyError，
机器人只会回"在忙，我稍后回复您"（7-25/26/27/29 各栽一次）。
所以模块顶层建了 `HTTP = requests.Session(); HTTP.trust_env = False`，
OpenAIAPI / DifyAPI / DusAPI 的请求全部走它。**合并上游后确认这些 `HTTP.post` 没被改回 `requests.post`。**
注意这个修复是 2026-07-30 03:19（commit 9facfe9）才落地的，**机器人进程不重启就还在走代理**。

### 3.13 UI 看门狗插件 `plugins/ui_watchdog/`
wxautox 的 UI 操作带全局 `@uilock`，内部循环一卡死锁永不释放，同进程所有微信操作永久阻塞，
Python 层杀不掉线程 —— 唯一出路是整进程重启。插件两种检测，都触发计划任务
`SWXPanelRestart`，重启后 `web_server.py` 读自启动标记自动拉起机器人：
1. **主循环心跳停滞**（300 秒判定卡死）——针对 @uilock 死锁类卡死。
2. **wxauto 日志「消息解析失败」连续出现**（2026-07-30 加）——针对 RDP 会话断开/重连后微信
   UIA 层异常：主循环活着、每 3 秒正常轮询（心跳正常，检测 1 抓不到），但每条真实消息的
   发送者属性都解析失败（`chatbox.py:743`），消息被静默丢弃，表现为"卡住不回复"，
   机器人线程重启无效。判定：窗口期（600 秒）内 ≥3 条「消息解析失败」且期间无成功解析的
   `[friend]获取到新消息`/`[self]获取到新消息`（成功一条即清零）。增量 tail
   `wxauto_logs/app_YYYYMMDD.log`，首次从文件末尾读起（历史日志不算）、跨天自动换文件、
   触发后有 300 秒冷却防止重启生效前重复触发。跑在看门狗自己的后台线程里，无额外 hook。
- hook 3 处：`wxbot_core.py` 主循环 `heartbeat()` / 退出时 `disarm()`，`web_server.py` 启动时 `consume_autostart_flag()`。
- **`schtasks` 必须用 `%SystemRoot%\System32\schtasks.exe` 绝对路径**：面板进程的 PATH 里可能没有
  System32，裸名字 `Popen` 直接 FileNotFoundError —— 2026-07-30 03:20 它就是这么哑火的
  （日志：`触发重启失败: schtasks not found`），看门狗白装了一晚。
- ★★ **触发重启 = 写自启动标记 + 调 `schtasks`，两步缺一不可**。写标记的实现是模块级
  `write_autostart_flag(reason, now)`（带 3 次重试和错误处理），`Watchdog._write_autostart_flag`
  和 `listen_health._trigger_restart` **共用同一份**。
  **任何新增的"触发 SWXPanelRestart"调用方都必须先调它** —— 只触发重启而不写标记，
  面板会起来、机器人却是停的，等于把机器人整个搞下线（2026-08-11 listen_health 就是这么
  丢了 1.5 小时，见 3.18）。
- ⚠️ **`tests/test_ui_watchdog.py` 在 win-shukong 上跑会【真的】触发重启**：用例会走到
  `schtasks /run /tn SWXPanelRestart`。在 mac 上跑是安全的（没有 `schtasks`，日志里会看到
  `触发重启失败: schtasks not found`）。**要在生产机上跑就先把 `_trigger` 换成 mock**，
  跟 `listen_health` 的 `TestProbe` 关掉 `auto_restart` 是同一个道理。
- 单测：`PYTHONPATH=. python3 tests/test_ui_watchdog.py`（26 个）。

### 3.15 上下文守卫插件 `plugins/context_guard/`（2026-07-30 加）★ 治"模型一本正经胡编"
起因：松爸私聊问"今天有什么 AI 新闻"，肥肉张口就编，还说自己"刚刷了刷 X（推特）"，
报出 Claude 3.5 Sonnet / Llama 3.1 / GPT-5 / Claude 4 / Gemini 2.0 一堆真假掺半的版本号。

做了对照实验（裸模型 vs 只加人设 vs 人设+真实历史），结论是**两边都有责任，但主因不在人设**：
- **上游**：`grok-4.5` 渠道在无 system、无历史的裸调用下，主动说"今天是 2024 年 10 月 24 日"，
  自称"我可以联网（使用实时网页搜索）"，认为最新的 Claude 是 3.5 Sonnet。编造是它的默认行为。
  同一中转站上 `glm-5.2` 也停在 2024/05，而 `minimax-m3` 知道现在是 2026 年、Claude 已到 4.5——
  **所以"grok-4.5"这个渠道名下挂的多半不是真的 Grok 4.5，值得按 zhongzhuan-ops 那套去查渠道映射。**
- **我们**：从没告诉过它今天几号、也没告诉它这里没有搜索工具，等于默许它按幻觉发挥。

插件干两件事，都是纯函数：
- `augment_prompt(base)`：在人设后追加「当前时间与能力边界」——动态日期
  + "宁可承认不知道也不要编版本号和新闻" + "不要复读上下文里的自我介绍和别人贴给你的文案"。
  **2026-08-03 改：原来写死"你没有联网/搜索/浏览器"，把 `grok-chat-fast` 这类真有搜索能力的模型
  也一起摁死了**（松爸问推特被答"我没法联网"，其实 grok 搜得到）。现在改成能力无关的说法——
  "遇到最新信息先用搜索工具查一遍，查到了照查到的说并附来源；压根没工具或没搜到就直说查不到"。
  一句话同时覆盖两种模型，机器人侧不必猜最终用的是谁（KB 会话由 rag_proxy 内部分流，它自己也不知道）。
  实测：`deepseek-v4-flash`（无搜索）照样老实认怂不编，核心目标没丢。
- `filter_history(msgs)`：清掉喂给模型的历史里的三类垃圾——① `attr=system` 的时间戳条目
  （content 就是 `"04:38"`，被当成用户发言喂进去）② API 兜底文案 `在忙，我稍后回复您`
  （作为 assistant 历史等于教模型这是个合法回复）③ 落进记忆的 `[NO_REPLY]` 标记。
  **只过滤送给模型的副本，记忆文件本身不动。**

hook 3 处，合并上游后逐个确认：
- `wxbot_core.py` `MemoryManager.get_messages`（约 1069 行）：返回前过一道 `filter_history`。
- `wxbot_core.py` `_get_chat_prompt` / `_get_group_prompt`（约 3450/3475 行）：所有 return 走 `self._guard_prompt(...)`，
  含 ncc_kb 的 KB 人设分支。`_guard_prompt` 就在这两个函数上面。

重放验证（同一历史、同一问题，改前 vs 改后）：编造新闻/版本号、"我刚刷了推特"全部消失，
被用户施压逼问时也不再改口去编。副作用：问天气这类它本来就不知道的事，现在会老实说"没联网查不到"
（这是对的，但语气比以前硬一点）。已知残留：用户把机器人自我介绍原文粘回聊天时，
偶尔还会复读一句"知识库还在更新中"——那是合法的用户消息，没法通用地过滤掉。

配置 `plugins/context_guard/data/config.json`（三个开关 + 兜底文案黑名单），默认值进库。
单测：`PYTHONPATH=. python3 tests/test_context_guard.py`（13 个，纯函数不发请求）。

**注意**：`memory_context_count` 目前是 1000（面板「上下文条数」），松爸那段对话实际喂了 40+ 条历史，
复读和跑偏跟这个也有关系，建议调到 20–30。

**★ 历史里的坏回复会被照着复读（2026-08-03 血泪，`filter_history` 又加了两条规则）**：
松爸连问三次"搜一下推特上 DeepSeek 的评价"，肥肉每次都答"我这边没法联网"。
查下来**路由完全正确**（qa 日志确认走的就是有搜索能力的 grok），病根是历史里那几条自我否定
被模型当成行为范例——**同一份历史去掉那几条再问，同一个模型立刻真搜并给出 x.com 引用**。
跟"在忙，我稍后回复您"是同一类病。踩到的两个坑：
- **分段回复要连坐**：`||SPLIT||` 拆出来的多条共享同一个 `time`，关键词只落在其中一条上。
  只丢那条，剩下的半句（"你把链接贴过来，我帮你提炼"）照样留在历史里当范例。
  现在按 time 把同一时刻的机器人发言一起丢。
- **必须整轮丢，只丢机器人那半边会更糟**：留下三条孤零零的重复提问，模型看到
  "同一个问题问了三遍都没人应"反而判定不用接话，**实测直接回了 `[NO_REPLY]`——静默不回复
  比答错更糟**。现在丢坏回复时把同一轮的用户提问一起摘掉。
- 子串黑名单 `drop_assistant_substrings`（没法联网/没有联网/无法联网/不能联网）**只对
  `attr=self` 生效**——用户问"你是不是没法联网"得留着。

### 3.16 分条回复形状插件 `plugins/reply_shape/`（2026-08-03 加）
治"一条回复拆成 6 个气泡、只有 3 个有内容"。上游的 `SPLIT_PROMPT_TEMPLATE` 只要求
"模仿真人拆分多条"，对每条的信息量没有任何约束，模型就拿开场白和收尾邀请去凑条数
（实测群里回了 6 条顶满上限：1 条铺垫 + 3 条有效 + 2 条"想看最新推文？""需要我继续挖？"）。

两层处理，**都不改上游那个模板**（它是模块级常量，改了合并上游必冲突）：
- `augment_split_prompt()`：在上游格式要求后、角色设定前插一段「每条都要有信息量」。
  治本（能理解语义、管得住开场铺垫），但模型不一定每次都听。
- `merge_thin_parts()`：发送前把短于 `min_chars`(20) 的碎片并进相邻条，合并后超过单条
  字数上限就不合。确定性兜底，只按长度判断——语义交给 prompt 那层。实测截图那 6 条 → 4 条。
- hook 2 处：`wxbot_core.py` 的 `_build_split_prompt` 与 `_parse_split_reply`。
  后者群聊私聊共用、单条字数上限却各配各的，取两者较小值传进去，两边都不会被撑破。
- `strip_markdown()`：剥掉微信渲染不了的 Markdown。**微信是纯文本的，模型写的 `**加粗**`
  会原样显示成一堆星号**。人设里干写"不要用 Markdown"管不住——实测散文式回答很干净，
  一到分类罗列（据点清单那种）就破戒，那是模型组织结构化信息时的本能。
  只剥三样：`**加粗**`、`#` 标题、`---` 分隔线；**留着** 行首 `- ` 列表符号（当纯文本读也清楚）
  和代码块（肥肉会讲技术，围栏剥了糊成一坨，块内 `__init__`、`a ** 2` 也一律不碰）。
  hook 在 `_clean_reply_for_send`，跟 `strip_leading_timestamp` 一样**不受 `clean_ai_reply_switch`
  开关控制**（这个转换对微信永远成立），且它在分条之前、群聊私聊两条路径都过。
- 配置 `plugins/reply_shape/data/config.json`（enabled / min_chars / extra_rule 可覆盖文案）。
- 单测：`PYTHONPATH=. python3 tests/test_reply_shape.py`（21 个，纯函数）。

### 3.17 人设文件已统一到 `config/prompt/肥肉.md`（2026-08-03）
以前是两套：`AI极客.md`（法斗 + AI 技术怪人，2779 字，人格鲜明）和 `NCC肥肉.md`
（社群助手，1502 字，偏客服）。同一个微信号在私聊和群里呈现两种人格，本身就分裂；
更浪费的是私聊全开知识库后全走 NCC肥肉，**写得更好的 AI极客 那套等于被架空了**。
另外接话判断在两个文件里各写一遍、措辞还不一样，改一个容易忘另一个。

现已融合成一份 `肥肉.md`。**融合的关键不是把两段拼起来，是加一层「什么时候收着点」**——
法斗的智识优越感（"我一只狗都知道 RAG 不是万能的"）对着社群新人问入住就很欠揍。
所以人设里专门写了一节：问据点/价格/联系方式/怎么报名、新人第一次提问、对方情绪不好，
这三种场合把玩笑收一收、准确优先；群里闲聊和技术辩论则性格全开。实测有效：
问 RAG 会说"它就像一只法斗的鼻子"，问据点则规规矩矩把三类据点讲清楚、只在中间带一句
"走着走着可能就撞见本狗了"。

配置三处一起指向它（改完要**重启机器人**，`config/config.json` 是启动时加载的）：
`config/config.json` 的 `default_prompt`、同文件 `group_prompt_map` 里原先指 `NCC肥肉` 的几个群、
`plugins/ncc_kb/data/config.json` 的 `prompt_name`。
- `🏜️AI 及其代理人联邦` 群配的是 `ai极客-冷酷版`，**不在融合范围内，别动**。
- `AI极客.md` / `NCC肥肉.md` 留在目录里但已无人引用，是回退用的底稿。
- 人设里那条"不要用 Markdown"**要把原因一起写上**（"会原样显示成一堆星号"），
  实测比干说一句"禁用 Markdown"管用得多。

### 3.14 wxautox4 版本、升级与 `SEARCH_CHAT_TIMEOUT`（`wxbot_core.py`）

**当前版本：41.1.1.post1**（2026-08-11 从 40.1.15 升上来，配 wx 客户端 4.1.9.35、Python 3.12.8）。

`SEARCH_CHAT_TIMEOUT`：40.1.15 装出来的默认值是 **2 秒**（不是文档说的 5）。`ChatWith` 在会话
列表里找不到目标就走搜索框，微信搜索结果常常 2 秒内还没渲染完，于是静默返回
`failure("未找到会话")`。我们改成 5 秒。**合并上游 / 升级 wxautox 后确认这行还在。**

#### ★★ 升级 wxautox4 前必读（2026-08-11 全套踩完）

- **PyPI 上 40.x 已全部下架**，`pip index versions wxautox4` 只剩 41.1.1 / 41.1.1.post1，
  **pip 回滚不了**。本地 pip 缓存里也没有旧 wheel。
  唯一退路是手工备份：`site-packages` 下的 `wxautox4/` + `wxautox4-<版本>.dist-info/`
  打包成 zip，出事解压盖回去（同机、同 Python、同平台，这么退是通的）。
  **40.1.15 的备份在 `C:\Users\Admin\wxautox4-40.1.15-backup-20260811.zip`（3.96 MB，55 个条目），别删。**
  升级前先打包、并且**验证 zip 能读**（列条目、确认 `.pyd` 齐全），没验证过的备份等于没有。
- ★★ **别在机器人跑着的时候升级它正在用的库**。pip 装完那一刻起，磁盘上是新版、
  进程内存里是旧版，而 Python 的 import 是**懒加载**的 —— 老进程一旦触发某个还没
  加载过的子模块，就会把新版代码塞进旧版运行时。这跟 2026-08-05 那次
  「迁移改了数据格式、没重启的老进程当场 TypeError」是同一类病（见 3.6）。
  正确姿势：**先停机器人 → 再升级 → 再启动**，别赌那段窗口没人发消息。
- **pip 删不掉被占用的旧目录时会改名**成 `~i` / `~sgs` / `~tils` 留在 `wxautox4/` 下
  （日志里是 `WARNING: Failed to remove contents in a temporary directory`）。
  它们不是合法模块名、Python 不会 import，属于纯垃圾，**等重启之后**再删。
- ★★ **升级后必须给面板启动脚本设 `PYTHONIOENCODING=utf-8`，否则 41.x 直接起不来。**
  见下条，这是本次升级唯一的硬阻断。

#### ★★ 41.x 的 emoji 日志 + GBK stdout = 机器人必然起不来（2026-08-11）

升级后机器人初始化 **100% 失败**，报
`'gbk' codec can't encode character '\U0001f436' in position 15`，
被 `init_wx_listeners` 的 except 接住，只留一句
**「初始化微信监听器失败，请检查微信是否启动登录正确」—— 完全指错方向**，
微信自始至终好好登着。中间三次手动重启全败在这上面，白耗一个多小时。

`\U0001f436` 是 🐶。崩的是 41.x **新增**的那句缓存日志：
`初始化成功，获取到已登录窗口：🐶肥肉（使用缓存）`
数到第 15 个字符正好是 🐶 —— 本机微信登录昵称就叫「🐶肥肉」。
`restart_panel.bat` 用 `>> panel_logs\panel_restart.log 2>&1` 把 stdout 重定向进文件，
Windows 下默认 GBK，编不了 emoji 直接抛异常。40.1.15 不打这句日志，所以以前没事。

已修：`restart_panel.bat` 里加了 `set PYTHONIOENCODING=utf-8`
（`C:\Users\Admin\swx_run.cmd` 早就有这一行，同一台机器上用它跑的脚本打同样这句话毫无问题）。
**以后新增任何会启动 Python 的 .bat / .cmd，都照抄这一行** —— 只要日志里可能出现
群名、昵称里的 emoji（我们满地都是🐶），迟早撞上。

- ★ **排查弯路，别重走**：一度以为是 `GetMyInfo()` 的 API 被破坏 ——
  `wx_id = my_info.get('id', f'{self.wx.nickname}')` 一旦回落到昵称，
  记忆目录就会从 `memory/FeiRou_NCC/` 变成 `memory/🐶肥肉/`，61 个会话的历史全部失联。
  **实测否掉了**：41.x 的 `GetMyInfo()` 返回
  `{"display_name": "🐶肥肉", "id": "FeiRou_NCC"}`，`id` 键健在，记忆目录没事。
  教训：别拿报错里的 `position N` 去凑猜想中的字符串，**把每个候选逐字数一遍**，
  一分钟就能定位到真正那一行。
- **诊断脚本必须在会话 2 跑**（`SWXRun` → `swx_run.cmd`，见 3.8）：任何要连微信的脚本，
  SSH 在 session 0 上 UIA 看不到微信窗口。输出写 UTF-8 文件再读，别指望控制台。

#### 41.x 升级后仍未解决的

- **`MoveWindow 1400`（监听窗口丢失）照旧存在**，2026-08-11 16:04 实测触发。
  上游 V4.7.30 版本日志写的"修复监听偶尔丢失系统消息的bug"**不是这个问题**，
  别再指望升级能治它 —— 兜底仍然靠 `plugins/listen_health/`（见 3.18）。

### 3.16 模型故障转移插件 `plugins/model_fallback/`（2026-08-03 加）★ 一个模型挂了自动换下一个
面板原来只能"选一个接口用"，挂了就直接回"在忙，我稍后回复您"。本插件让接口失败时
自动沿备用链换模型、拿同样的上下文重答同一个问题，用户不用重发。

- **原理**：`FallbackAPI` 包装器，对外暴露与四个 API 类一致的 `.chat()` 签名，内部按顺序试。
  历史/人设/分段/图片/接话闸门全走上游原链路，只换"这次用哪个接口"。
- **hook 2 处**，在 `wxbot_core.py` 的 `_get_group_api` / `_get_chat_api`：原逻辑（含 ncc_kb hook）
  整体挪进 `_resolve_group_api` / `_resolve_chat_api`，入口函数只剩一行 `_with_fallback(...)`。
  这么拆是为了让上游改接口选择逻辑时，冲突落在函数体内部、diff 上下文一致，merge 能自动过。
  **合并上游后确认这两个入口和 `_with_fallback` 还在。**
- **失败判定**：底层四个 API 类把异常吞成固定串 `"API返回错误，请稍后再试"`（7 个 return 点），
  **状态码根本没往上传**，所以第一版不分 500/403/529，凡失败就切下一个。代价是 403（key 废了）
  也会白试一次备用。要精确分流就得改那 7 个 return 点，冲突面变大，暂不值当。
  判定失败 = 抛异常 / 返回那个固定串 / 返回空白。全链失败时**交回同一个固定串**，
  上层照旧走 `api_error_reply`，行为与没装插件时完全一致。
- **配置在 `config/config.json`**（不是插件自带文件）：`fallback_switch` + `fallback_chain`
  （api_configs 的索引数组，有序）。面板就在原来的「API 接口配置」卡片里勾「备用」，
  索引由前端与 `api_configs` **同一次遍历**生成，增删接口不会错位。
  **和改 api_configs 一样，改完要重启机器人线程才生效。**
- 顺带处理了两个坑：备用接口签名不兼容时按 `inspect.signature` 裁掉多余 kwarg
  （Dify/Coze 的 `chat()` 没有 `image_path`，原样透传会 TypeError 白费一个备用）；
  按 `(base_url, 模型, key 前 8 位)` 去重，避免链上和当前会话用的是同一个接口时重复试。
- 单测：`PYTHONPATH=. python3 tests/test_model_fallback.py`（26 个，纯 mock 不发请求）。
  `chain.py` 刻意不 import `wxbot_core`（会连带拉起 wxautox），所以 mac 上能裸跑。
- 设计与决策记录：`plugins/model_fallback/SPEC.md`。

### 3.18 动态监听「窗口丢失」与自愈 `plugins/listen_health/`（2026-08-04 加）★ 治私聊消息静默丢失

**故障链条**：全局模式下来了新私聊 → `AddListenChat` 给它开独立聊天窗口 →
**窗口没弹出来** → wxautox 拿到 0 句柄 → `wxautox4/ui/base.py` 的 `set_window_size()`
里 `win32gui.MoveWindow(0, ...)` 抛 `error(1400, 'MoveWindow', '无效的窗口句柄。')`。
上层重试 3 次全败 → 回落主窗口（`MainWindowChat`）。

**它到底是什么**（2026-08-04 问了 wxautox 作者 Siver）：就是普通的**微信窗口丢失**，
可能是微信/Windows 自身的问题，也可能被人为或后台软件干扰，**碰到了重启就好**。
我们的日志完全对得上，而且比作者说的更乐观——**只重启我们的进程就够，微信客户端不用重启**
（所以自愈不需要人扫码，可以全自动）：

| 时间 | 事件 |
|------|------|
| 08-03 20:34 / 20:37 | 连续两次失败（其间 21:24 朋友圈点赞正常 → 坏的只是「开独立窗口」这一个能力，不是整个 UIA）|
| 08-04 00:20 | 重启程序 → 初始化 5 个监听全部成功 |
| 08-04 00:49 / 08:04 | 成功 |
| 08-04 13:46 | 失败 |
| 08-04 15:09 | 重启程序 → 诊断脚本连打 18 轮全成功 |

**关键性质：进坏状态后持续失败，不是随机抖动。** 别再按「5% 概率随机」去理解——
连续两次失败在独立随机下只有 0.25% 的概率；15:09 之后那 18 轮全过，是因为测在重启之后，
测的是健康状态，什么也没证明。

#### ★ 三个已被证伪的假说，别再重走

1. **「微信独立聊天窗口有 5 个上限」** —— 手动开到 8 个，第 9 个照样开得出来；
   08-04 00:49 和 08:04 都是在 5 个窗口占满时成功的。
2. **「朋友圈点赞等 UI 操作把状态搞脏」** —— 只对得上 08-04 13:46 那次（相隔 52 秒），
   08-03 20:34 那次距上次点赞 2 小时 43 分。专门跑了 6 轮「开关朋友圈后立刻调用」，6/6 成功。
3. **「wxautox/UIA 连接随进程运行时长老化」** —— 成功样本覆盖 0~49.84 小时，
   失败样本 1.15/1.20/13.44 小时，完全重叠。

#### 四层处理

1. **回落通道补 `chat_type`**（`wxbot_core.py` 的 `MainWindowChat`）——
   这是**真正丢消息的那个 bug**。回落对象原来只有 `who` 和 `SendMsg`，而
   `process_message` 在全局模式分支里直接读 `chat.chat_type` → `AttributeError` →
   整条 `ALLListen_mode` 被 main 的兜底 except 接住，而消息已被 `GetNextNewMessage`
   消费掉、不会重投。**实际丢了两条真实消息**：08-03 20:34 King_🐕 的「签到」、
   08-04 13:46 基司菲尔的提问，用户什么回复都没收到。
2. **重试改退避**（`wxbot_core.RETRY_BACKOFF = (2, 5, 15)`）——原来固定 0.5 秒、
   4 次全挤在 16 秒内打完。对付间歇性 UI 故障缺的是时间不是次数。
3. **失败告警**（`alert.py`）——最终失败推 webhook + 管理群，同一会话 10 分钟冷却防刷屏。
   在此之前失败只写一行 ERROR 日志，两次丢消息都是人肉翻日志事后才发现的。
4. **探针 + 自愈**（`probe.py` / `heal.py`）——每 10 分钟对「文件传输助手」打一次
   `AddListenChat`，连续失败 2 次（≈20 分钟）判定坏状态 → 触发 `SWXPanelRestart`
   重启进程 → 冷却 60 分钟防重启风暴。**冷却期内又连续失败 = 重启这条路无效 =
   微信侧坏了 → 升级告警叫人**（这时候才轮到人上机重启微信客户端）。
   故障通常在 10~20 分钟内自愈，且赶在真人发消息之前。

采样落 `data/probe-YYYYMMDD.jsonl`（一天 144 条），除成败/耗时/异常外还记**环境快照**：
前台窗口标题+进程、RustDesk ESTABLISHED 连接数、微信主窗口 hwnd。
作者提的「人为/后台软件干扰」从此可查而不是靠猜。
（RustDesk console 模式**不写** TerminalServices 事件日志——已验证 08-03 12:00 起
一条会话事件都没有——所以只能从进程连接数看有没有人连着。
微信主窗口 hwnd 也值得盯：08-04 实测它会在**进程不重启**的情况下被销毁重建，
264072 → 64685408，而 wxautox 是启动时把它缓存下来用的。）

#### hook 5 处（合并上游后逐个确认）

- `MainWindowChat.__init__`：`chat_type='friend'`（少这一个属性就丢消息）
- `_add_listen_chat_once`：记 `self._last_listen_error`，供告警引用
- `_add_and_verify_subwindow`：退避重试 + 最终失败调 `alert_listen_failure`
- `init_wx_listeners` 定时任务注册处：挂探针 `register(self, schedule)`
- 主循环 `run_pending` 条件：加 `_listen_probe_enabled`

#### 坑

- ★ **跑单测可能触发真实重启**：探针的失败用例会把连续失败数推过自愈阈值。
  `tests/test_listen_health.py` 的 `TestProbe` 里已显式关掉 `auto_restart`，
  **以后新增探针相关用例照做**，否则在 win-shukong 上跑单测会真的重启机器人。
- **自愈状态必须落盘**（`data/heal_state.json`）：进程被杀后内存全丢，
  不落盘会陷入无限重启。
- **重启触发复用 `ui_watchdog._default_trigger`**，别自己再写一遍 —— 那里踩过
  `schtasks` 裸名字 `FileNotFoundError` 的坑（见 3.13）。
- ★★ **自愈 = 写自启动标记 + 触发重启，只做后半截等于把机器人搞下线（2026-08-11 血泪）**：
  `_trigger_restart` 原来只调了 `_default_trigger`，**漏了 `write_autostart_flag`** ——
  那段注释还写着"直接复用 ui_watchdog 的实现，别自己再写一遍"，schtasks 的坑是避开了，
  标记这一半却漏了。8-11 16:05 首次真触发自愈：探针判定窗口丢失 → 重启 → **面板起来了、
  机器人没被拉起**，一路下线到人发现为止（1.5 小时），期间消息全部无人应答。
  **比不自愈更糟**：本来只是监听坏了（丢部分消息），自愈之后变成机器人整个没了（丢全部消息），
  而且没有任何告警说"我重启完但没起来"。
  现已改为两步都做，回归测试见 `TestTriggerRestartWritesAutostartFlag`。
- ★ **`TestHeal` 会污染模块级函数，写新用例小心**：它的 `setUp` 把 `_trigger_restart` /
  `_alert` / `load_state` / `save_state` 四个名字全替换成 mock。原来没有 `tearDown`，
  跑完模块里就留着一堆假货，**任何想测「真」`_trigger_restart` 的用例都会静默测到 mock 上**
  （新增的回归测试单跑全绿、全量跑全红，就是撞的这个）。已补 `tearDown` 逐个还原。
  **以后 setUp 里替换模块级名字，一律配套 tearDown 还回去。**
- **探针绝不能另起进程/线程**跑微信 UI 操作，只能挂在 bot 的 schedule 主循环里串行执行
  （见 3.11 的血泪）。靶子用「文件传输助手」这种系统会话，不打扰真人、不产生已读。
- `data/` 整个不进库（默认值写在 `config.py` 的 `DEFAULTS`，文件缺失自动回落）。
  注意 `.gitignore` 第 4 行有全局 `config.json` 规则，插件的默认配置文件放不进库。
- 单测：`PYTHONPATH=. python3 tests/test_listen_health.py`（37 个，纯 mock）、
  `tests/test_main_window_chat.py`（8 个，用 ast 摘类出来 exec，不 import wxbot_core；
  其中一个用例会扫 `process_message` / `wx_send_ai` / `message_handle_callback` 里所有
  `chat.xxx` 读取，逐个校验回落通道答得上来——以后再漏属性直接测试失败）。

**根因仍未知**，已把现象整理成一段话发社区问了。探针数据攒够之后回来看失败样本的共同点：
失败那一刻 `fg_proc` 是谁、`rustdesk_conns` 是不是 0、`wx_main_hwnd` 有没有变。

---

## 4. 同步上游的标准流程

做过很多次（V4.7.23 → V4.7.30），套路固定：

1. `git fetch upstream --tags --prune`，看 `git diff --stat HEAD..upstream/main`。
   ⚠️ 这个 diff 会把**我们有、上游没有的插件全列成"删除"**（方向造成的假象，merge 不会删）。
   要看上游到底改了什么，得用 merge base：
   `git diff --stat $(git merge-base HEAD upstream/main)..upstream/main`。
2. 在当前自用分支上 `git merge --no-ff upstream/main -m "merge upstream Vx.x.x with local customizations"`。
3. **冲突处理原则：定制区一律以我们的版本为准。** 重点检查 `wxbot_core.py`、`web_server.py`、`templates/dashboard.html`、`webhook_send.py`。
4. 合并后按第 3 节逐项确认定制点还在，特别是：
   - `web_server.py` 的 `app.run(host='0.0.0.0', ...)` 没被改回 `127.0.0.1`（见 3.1）；
   - `wxbot_core.py` 的 wechat_checkin hook 还在（见 3.2）；
   - DusAPI 定制按 `AI_COLLABORATION_GUIDE.md` 第 4.5 节清单逐项过，删掉重新冒出来的 DusAPI 广告（见 3.5）。
5. 若上游要求 `wxautox4` 升级（历史上 40.1.14 → 40.1.15、40.1.15 → 41.1.1.post1），
   **先读 3.14 那一节再动手** —— 旧版从 PyPI 下架回滚不了、必须先手工备份 zip、
   必须先停机器人再升级、升完要确认 `PYTHONIOENCODING=utf-8`。别直接 `pip install -U`。
6. 验证：
   ```
   python -m py_compile wxbot_core.py web_server.py webhook_send.py plugins/wechat_checkin/handler.py plugins/wechat_checkin/store.py
   python -m unittest tests.test_wechat_checkin tests.test_webhook_send -v
   ```
   **外加一条查重复定义**（上游改写历史时必查，见下）：
   ```bash
   python3 -c "
   import ast,collections
   t=ast.parse(open('wxbot_core.py',encoding='utf-8').read())
   for n in ast.walk(t):
       if isinstance(n,(ast.ClassDef,ast.Module)):
           names=[x.name for x in n.body if isinstance(x,(ast.FunctionDef,ast.ClassDef))]
           for k,c in collections.Counter(names).items():
               if c>1: print('重复:',getattr(n,'name','<module>')+'.'+k,c)"
   ```
7. **运行时数据别乱提交**：`checkin.sqlite3`、`imports/*.json`、`config/config.json` 里的密钥都保持工作区状态，不进 commit（2026-07-30 起已由 `.gitignore` 兜住，见第 5 节）。
8. commit 后清楚汇报改了啥 + commit ID。

### ★★ 上游会 force-push 重写历史，合出来的"新功能"可能是我们早有的（2026-08-11）

合 V4.7.30 时撞上：**V4.7.28（tag `3e63e09`，我们 7-28 就合过）被从 `upstream/main` 上摘掉了**，
它的内容重新打包进了 V4.7.29。于是 merge base 从 V4.7.28 退回 **V4.7.27**，
git 把我们早已拥有的「关键词回复引用/@」整套当成上游新改动**又放了一遍**。

- **怎么认**：`docs/version.json` 里我们的版本号比 merge base 还新（我们 V4.7.28 vs base V4.7.27）；
  `git log --all --source -S'V4.7.28' -- docs/version.json` 能看到那个 tag 还在、但已不在 main 的历史里。
- **后果**：同名方法被合进来两份 —— 这次是 `WXBot._send_group_reply` 和 `_send_keyword_reply`
  各两份（我方一份、上游重放一份，**逐字节相同**）。Python 只让后一份生效，
  所以功能没差异、`py_compile` 也不报错，**属于静默冗余**：下次改代码时改了前一份会纳闷怎么不生效。
  所以第 6 步那条查重复定义的命令不是可选项。
- **反过来也是个好信号**：`dashboard.html` / `web_server.py` 因两边内容完全一致而自动合并、
  **净变化为零** —— 看到这个就说明"上游这版对这些文件的改动我们本来就有"。
- **处理原则不变**：定制区一律以我们的为准；重复定义删掉后加进来的那份。

历史依赖坑：`dingtalk-stream` 和 `websockets` 版本不兼容（15.0.1 崩，降到 14.2 好了）——这是 gateway 层的，不是本项目，但同一台机器上都遇到过。

---

## 5. Git 管理约定（2026-07-30 立）

之前的状态是"代码在生产机上跑着，版本库里没有"——`plugins/ai_news_note/` 整个插件写了三周半没进库，
`wxbot_core.py` 的代理修复也一直躺在工作区。这一节是为了别再发生。

### 5.1 分支：2026-08-03 起统一到主干
| 分支 | 角色 |
|------|------|
| `main` | **唯一的开发分支**，生产工作树（`C:\Users\Admin\SiverWXbot_plus-main` / mac 侧 `/Volumes/SiverWXbot_plus-main`）就检出在这里，日常提交推 `origin/main` |
| `upstream/main` | 上游只读，通过 merge 进来 |

**别再切回 `custom/webhook-integration-20260506`**——它是历史遗留名，已于 2026-08-03 合进 main 后停用。
之前"生产跑 custom、提交推 main"的两头状态制造过一次分叉：custom 上的 ncc_kb 私聊通配和
rag_proxy 分流两个功能只在本地、既没进 main 也没推 GitHub，差点在切分支时被漏掉。

`master` / `feat/webhook-notifications` / `update-upstream-b59692f` 是早期僵尸分支，别在上面干活。
`claude/*` 是各次会话的 worktree 分支，合进 main 后即可忽略。

**多会话同时干活时**：生产工作树是共享的，切分支/合并前先 `git status` 确认干净，
看到不是自己的未提交改动就停下来问，别 stash 也别覆盖——那多半是另一个会话正在写。

### 5.2 行尾：不要手动折腾
`.gitattributes` 已声明 `* text=auto`（仓库统一存 LF）+ `*.bat/*.cmd/*.ps1 eol=crlf`。
**别再加 `core.autocrlf`，也别手动转换文件行尾** —— 没这个文件时 mac 侧看 `git diff` 会显示 12000+ 行
假改动，`.bat` 也会被写成 LF 让 CMD 断错句。

### 5.3 什么不进版本库
`.gitignore` 里按用途分了区。核心原则：**真相源在别处的东西不进库**——
签到码池（真相源 hkbohai）、Notion 同步下来的 registry.json、面板写盘的运行配置、
`*.bak-*` 手工备份、日志、`config/config.json`。
改 `.gitignore` 后用 `git ls-files -i -c --exclude-standard` 查有没有"已跟踪但按规则该忽略"的漏网之鱼。

**例外：人设 `config/prompt/*.md` 进库（2026-08-03 起）。** 原来整个 `/config/` 被排除，
版本库里一个人设文件都没有——人设是产品的脸，改错了没法回退、机器一还原就没了。
但 `config/` 下几乎全是密钥和本机状态（`config.json` 及其 `.bak`、`panel_secret.key`、
`admin.json`、`email.txt`、`webhook.json`、`reply_count.json`），所以只精确放行 `.md`：
```
/config/*
!/config/prompt/
/config/prompt/*
!/config/prompt/*.md
```
**gitignore 的规矩是父目录被排除后子文件的 `!` 例外不生效**，只能像上面这样逐层放行，
末尾用 `!*.md` 收口（以后 prompt/ 下出现 `.bak` 之类也不会误入库）。
往库里放 `config/` 下任何东西之前，先跑这三步验一遍：
`git check-ignore -v` 逐个确认密钥文件仍被挡住 → `git add --dry-run` 看实际会加哪些 →
grep 一遍 `sk-` / `api_key` / `Bearer` / `password` 确认文本里没夹带密钥。

### 5.4 提交节奏
- **一件事一个 commit**，标题写清楚"改了什么"，正文写"为什么"——尤其是踩坑改的，把日期和现象写进去
  （这个库的 commit message 就是事故档案）。
- 生产机上验证完的改动**当天提交**，别攒。攒着的代价是：机器一还原、目录一同步，人就没了。
- 提交前跑 `python3 -m py_compile` + 相关单测（mac 上直接跑文件，见 5.5）。

### 5.5 mac 上跑单测
anaconda 自带的 `tests` 包会遮蔽本项目的 `tests/`，`python -m unittest tests.xxx` 会导错模块。正确姿势：
```bash
cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_ncc_community.py
```

### 5.6 mac 侧走 SMB 挂载时先设 `core.checkStat minimal`
`/Volumes/SiverWXbot_plus-main` 是 smbfs 挂载，git 的 stat 缓存（inode/ctime）在网络盘上对不上，
会误判整个工作区是脏的——表现是 `git status` 显示干净、`git merge` 却报 `fatal: stash failed`。
在这份 checkout 上执行一次即可（本地配置，不进库）：
```bash
cd /Volumes/SiverWXbot_plus-main && git config core.checkStat minimal && git config core.trustctime false
```

### 5.7 凭据
`origin` 的 remote URL 里曾明文嵌 GitHub token。**别把 token 写进 remote URL**，用 SSH 或
credential helper；一旦写过就当它已泄露，去 GitHub 吊销重发。

---

## 6. 干活的通用准则

- 注释和 UI 文字保持中文，遵循 dashboard 现代简约风格。
- 改 `config/config.json` 结构时，确保前端能处理默认值缺失，别让页面渲染崩。
- 新功能优先做成插件 / 最小 hook，别往 `wxbot_core.py` 里塞大逻辑。
- 改完先本地 `py_compile` + 单测，再提交。
- 涉及重启才生效的改动，先跟用户说。

---

## 7. 关键路径速查

| 用途 | 路径 |
|------|------|
| 主入口（Flask 面板） | `web_server.py` |
| 机器人核心 / AI 调用 / 插件 hook | `wxbot_core.py` |
| 手动启动 | `manual_start_bot.py` |
| 整进程重启脚本（计划任务 `SWXPanelRestart` 跑它） | `restart_panel.bat`（★ 含 `set PYTHONIOENCODING=utf-8`，缺了 41.x 起不来，见 3.14） |
| wxautox4 40.1.15 回滚备份 | `C:\Users\Admin\wxautox4-40.1.15-backup-20260811.zip`（PyPI 已下架 40.x，这是唯一退路，别删） |
| Webhook 发送 | `webhook_send.py` |
| 签到插件 | `plugins/wechat_checkin/` |
| NCC 社群插件 | `plugins/ncc_community/`（面板 `/ncc_community` + `panel.py`，去 Notion 化见 3.6 与 `PANEL_SPEC.md`） |
| 知识库开关插件 | `plugins/ncc_kb/` |
| AI 日报插件 | `plugins/ai_news_note/` |
| 监听健康 / 自愈插件 | `plugins/listen_health/`（探针采样 `data/probe-*.jsonl`，见 3.18） |
| 码池拉取导入 | `plugins/wechat_checkin/pull_and_import.py`（计划任务 `WechatCheckinPull` 每天 8:05） |
| 面板模板 | `templates/dashboard.html` |
| 配置 | `config/config.json` |
| 日志 | `panel_logs/`、`wxauto_logs/` |
| 测试 | `tests/`（mac 上直接跑文件，见 5.5） |
| 行尾 / 忽略规则 | `.gitattributes`、`.gitignore`（见第 5 节） |
