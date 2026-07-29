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
- 长期自用分支：`custom/webhook-integration-20260506`（当前工作分支）

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
- 拉群关键词真相源是 Notion「迎新拉群」表（让对方回复→拉入群聊），管理群发「同步」拉进 registry.json 的 `invite_keywords`；「设拉群」是本地覆盖（同名优先）。**拉群只在私聊触发**，群聊发关键词不处理（2026-07-12 定）。
- 指令表见群里发「帮助」，或 `forward.py` 的 HELP_TEXT。带空格的群名用 `|` 分隔参数。
- 运行配置在 `plugins/ncc_community/data/config.json`（分组/迎新/拉群关键词），群内指令实时写盘；**别提交该文件的运行时变更**。
- 机器人程序化回复统一带 `🤖` 前缀，指令层据此忽略自己的消息防自触发循环——别去掉这个前缀。
- **wxautox4 40.1.15 实测 `msg.forward()` 成功时返回 None**（与文档 WxResponse 不符），`forward.py` 里已按此处理，升级 wxautox 后留意这行为。
- 单测：`python -m unittest tests.test_ncc_community`（24 个，纯 mock 不碰微信）。

### 3.7 AI 问答知识库（mac-mini，2026-07-05 加）
知识库栈在 `mac-mini:~/ncc-kb/`（Qdrant + rag_proxy，launchd 常驻），355 篇公众号文章 2175 块，端点 `http://100.71.182.5:8434`（Tailscale，OpenAI 兼容）。
**话题闸门**（2026-07-05 加）：rag_proxy 用向量检索最高分判定是否 NCC 话题——达标（默认 `KB_SCORE_THRESHOLD=0.38`）才挂知识库+facts；非 NCC 提问（写代码/闲聊/时事）跳过检索，纯人设正常问答，也更快。实测分布 NCC 0.43~0.52 / 通用 0.20~0.35，日志里有每次 `[gate]` 决策。改阈值后 `launchctl kickstart -k gui/501/com.ncc.ragproxy`。
固定事实（据点/主理人/联系方式）在 `mac-mini:~/ncc-kb/facts.md`，命中 NCC 话题时随上下文注入，改完即时生效（2026-07-05 已填真实内容：大理/黄山黟县黑多岛/三亚崖州三据点、主理人大曹 ShariCao、小助手 nccxiaozhushou 等，含时效声明）。
补语料：md 丢进 corpus/ 跑 `venv/bin/python ncc_ingest.py`。健康检查 `curl http://100.71.182.5:8434/health`（含当前阈值）。

### 3.9 知识库开关插件 `plugins/ncc_kb/`（2026-07-06 加）★ 让群聊/私聊可选接入知识库
不再靠 `config.json` 的 api_configs 索引挂知识库（面板保存 api_configs 会把它冲掉，踩过），改由本插件独占 KB 路由，自带端点，抗面板改动。
- **原理**：`wxbot_core.py` 四个 getter（`_get_group_api`/`_get_chat_api`/`_get_group_prompt`/`_get_chat_prompt`）各加一段最小 hook，先问插件"这会话开知识库了吗"——开了返回 KB 接口实例 + `NCC肥肉` 人设；没开走上游原逻辑。复用上游整条 AI 链路（历史/分段/图片），只换"用哪个接口+人设"。合并上游后确认这 4 处 hook 还在。
- **开=走 KB 端点+NCC肥肉人设；关=回落到该会话原本的 group_api_map/默认接口**（所以测试群已从 group_api_map 移除，靠插件路由；否则"关"会因残留 index 仍连 KB）。
- 配置 `plugins/ncc_kb/data/config.json`（endpoint / prompt_name / enabled_groups / enabled_chats），面板 `/ncc_kb` 页可视化增删，改动下一条消息即生效，无需重启。**别提交该文件的运行时变更**。
- **面板**：独立模板 `templates/ncc_kb.html` + `web_server.py` 三个新路由（`/ncc_kb`、`/ncc_kb/config`、`/ncc_kb/save`）+ dashboard.html 侧栏一行链接（都是新增，冲突面极小）。
- 单测：`python -m unittest tests.test_ncc_kb`（9 个，纯 mock）。私聊在全局模式也能开（补齐上游 chat_api_map 只在白名单模式生效的空缺）。

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

### 3.12 AI 调用绕开系统代理（`wxbot_core.py` 模块级 `HTTP` 会话）
这台 Windows 的 IE 系统代理指向局域网某台机器的 `:7897`（换过段：`192.168.3.5` → `192.168.1.2`/mac-mini），
`requests` 在 Windows 会自动读注册表跟着走。那台机器一休眠，AI 调用全线 ProxyError，
机器人只会回"在忙，我稍后回复您"（7-25/26/27/29 各栽一次）。
所以模块顶层建了 `HTTP = requests.Session(); HTTP.trust_env = False`，
OpenAIAPI / DifyAPI / DusAPI 的请求全部走它。**合并上游后确认这些 `HTTP.post` 没被改回 `requests.post`。**

---

## 4. 同步上游的标准流程

做过很多次（V4.7.23 → V4.7.27），套路固定：

1. `git fetch upstream --tags --prune`，看 `git diff --stat HEAD..upstream/main`。
2. 在当前自用分支上 `git merge --no-ff upstream/main -m "merge upstream Vx.x.x with local customizations"`。
3. **冲突处理原则：定制区一律以我们的版本为准。** 重点检查 `wxbot_core.py`、`web_server.py`、`templates/dashboard.html`、`webhook_send.py`。
4. 合并后按第 3 节逐项确认定制点还在，特别是：
   - `web_server.py` 的 `app.run(host='0.0.0.0', ...)` 没被改回 `127.0.0.1`（见 3.1）；
   - `wxbot_core.py` 的 wechat_checkin hook 还在（见 3.2）；
   - DusAPI 定制按 `AI_COLLABORATION_GUIDE.md` 第 4.5 节清单逐项过，删掉重新冒出来的 DusAPI 广告（见 3.5）。
5. 若上游要求 `wxautox4` 升级（历史上 40.1.14 → 40.1.15 配合微信 PC 版本），同步 pip 升级。
6. 验证：
   ```
   python -m py_compile wxbot_core.py web_server.py webhook_send.py plugins/wechat_checkin/handler.py plugins/wechat_checkin/store.py
   python -m unittest tests.test_wechat_checkin tests.test_webhook_send -v
   ```
7. **运行时数据别乱提交**：`checkin.sqlite3`、`imports/*.json`、`config/config.json` 里的密钥都保持工作区状态，不进 commit（2026-07-30 起已由 `.gitignore` 兜住，见第 5 节）。
8. commit 后清楚汇报改了啥 + commit ID。

历史依赖坑：`dingtalk-stream` 和 `websockets` 版本不兼容（15.0.1 崩，降到 14.2 好了）——这是 gateway 层的，不是本项目，但同一台机器上都遇到过。

---

## 5. Git 管理约定（2026-07-30 立）

之前的状态是"代码在生产机上跑着，版本库里没有"——`plugins/ai_news_note/` 整个插件写了三周半没进库，
`wxbot_core.py` 的代理修复也一直躺在工作区。这一节是为了别再发生。

### 5.1 三条分支
| 分支 | 角色 |
|------|------|
| `origin/main` | **我们的定制主线**，日常提交推这里 |
| `custom/webhook-integration-20260506` | 历史遗留的长期分支名，与 main 同步 |
| `upstream/main` | 上游只读，通过 merge 进来 |

`master` / `feat/webhook-notifications` / `update-upstream-b59692f` 是早期僵尸分支，别在上面干活。

### 5.2 行尾：不要手动折腾
`.gitattributes` 已声明 `* text=auto`（仓库统一存 LF）+ `*.bat/*.cmd/*.ps1 eol=crlf`。
**别再加 `core.autocrlf`，也别手动转换文件行尾** —— 没这个文件时 mac 侧看 `git diff` 会显示 12000+ 行
假改动，`.bat` 也会被写成 LF 让 CMD 断错句。

### 5.3 什么不进版本库
`.gitignore` 里按用途分了区。核心原则：**真相源在别处的东西不进库**——
签到码池（真相源 hkbohai）、Notion 同步下来的 registry.json、面板写盘的运行配置、
`*.bak-*` 手工备份、日志、`config/config.json`。
改 `.gitignore` 后用 `git ls-files -i -c --exclude-standard` 查有没有"已跟踪但按规则该忽略"的漏网之鱼。

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
| Webhook 发送 | `webhook_send.py` |
| 签到插件 | `plugins/wechat_checkin/` |
| NCC 社群插件 | `plugins/ncc_community/` |
| 知识库开关插件 | `plugins/ncc_kb/` |
| AI 日报插件 | `plugins/ai_news_note/` |
| 码池拉取导入 | `plugins/wechat_checkin/pull_and_import.py`（计划任务 `WechatCheckinPull` 每天 8:05） |
| 面板模板 | `templates/dashboard.html` |
| 配置 | `config/config.json` |
| 日志 | `panel_logs/`、`wxauto_logs/` |
| 测试 | `tests/`（mac 上直接跑文件，见 5.5） |
| 行尾 / 忽略规则 | `.gitattributes`、`.gitignore`（见第 5 节） |
