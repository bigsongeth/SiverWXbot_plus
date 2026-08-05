# PANEL_SPEC — NCC 社群管理迁入自家面板（去 Notion 化）

> 状态：**P1–P3 已实现**（2026-08-05），P4 生产验收待做（见文末「实现记录」）。
> 决策背景：Notion 维护关键词/拉群/分组不方便，且依赖微信对话指令；全部改为在
> Flask 面板里维护。参照 ncc_kb（CLAUDE.md 3.9）的"独立模板 + 少量新路由 +
> 侧栏一行链接"模式，对上游零侵入。

## 0. 完成标准（验收判据）

1. 面板新页 `/ncc_community` 能完成今天所有靠 Notion + 微信指令做的维护：
   群的分组/允许转发/允许发言/迎新链接、分组编号管理、拉群关键词增删、
   待归类新群的归类、不可达群的人工恢复。
2. 机器人侧转发/拉群/迎新行为不变（现有 92+36 个单测全绿，新增面板路由测试）。
3. 「同步」不再访问 Notion；断网（Notion 侧）不影响任何功能。
4. 生产实测：面板改一个拉群关键词 → 微信私聊发该关键词 → 正确拉群，全程不碰 Notion。

## 1. 现状：Notion 承担的 9 个触点（迁移清单）

| # | 触点 | 位置 | 迁移后 |
|---|------|------|--------|
| 1 | 「同步」拉取分组/群/关键词 | forward.py:836 → notion_sync.pull | 删除。面板直接写 registry.json |
| 2 | 新群发现回写 Notion 待归类 | discovery.py:63 push_discovery | 删除。registry.add_pending 已落本地，面板「待归类」页展示 |
| 3 | 「回写notion」批量改标题 | batch.py:141 run_notion_pass | 下线该指令 |
| 4 | 修备注后回写群名 | forward.py:1202 _sync_names_to_notion | 删除 |
| 5 | 「后台」菜单发 Notion 链接 | forward.py:657 NOTION_BACKEND_URL | 改发面板地址（Tailscale URL） |
| 6 | 改名迁移按 notion_page_id 认人 | registry.upsert_from_notion:339 | 换内部稳定 id（见 §2） |
| 7 | remark_applied 的「Notion 标题带🐶」兜底 | registry.py:352 | 删除该兜底（假绿来源，8-4 事故根因之一）。以本地 remark_applied 为准，丢失可用「修备注 全部」重建 |
| 8 | 拉群关键词表（迎新拉群） | invite_keywords ← Notion 表 | 面板 CRUD；与「设拉群」的 remark_overrides 合并成一张表 |
| 9 | 不可达群靠「修 Notion + 同步」复活 | upsert_from_notion 无条件覆盖 | 面板一键恢复（顺手修掉"同步悄悄复活坏群"的洞） |

## 2. 数据模型（registry.json 自立为唯一真相源）

现有字段基本够用，只做三个调整：

- **加 `gid`**（内部稳定 id，uuid4 前 8 位）：接替 notion_page_id 的"改名认人"
  职责。改名走面板专用"改名"操作：换 key、继承 remark（微信里真实备注）、保 gid
  ——复刻 upsert_from_notion 那套迁移逻辑，但由人在面板上显式触发，不再有
  同步时的隐式批量迁移。
- **`notion_page_id` 保留为只读遗留字段**，不再使用（回滚保险）。
- **`invite_keywords` 升级为结构化**：`{keyword: {"group": 群名, "enabled": true}}`,
  迁移时把 config.json 的 `remark_overrides`（设拉群本地覆盖）合并进来，
  同名冲突以 remark_overrides 为准（它本来就是"同名优先"的覆盖层）。

并发安全：面板和 bot 同进程（web_server 起 bot 线程），面板路由直接
`from plugins.ncc_community import registry`，共享 registry._LOCK，无跨进程竞争。
registry.py 纯 Python 无 wxautox 依赖，bot 未启动时面板页照常可用。

新增 registry CRUD（全部在 _LOCK 内）：
`set_group_fields(name, **fields)` / `rename_group(old, new)` /
`set_grouping(name, number, forward_enabled)` / `delete_grouping(name)` /
`set_invite_keyword(kw, group)` / `delete_invite_keyword(kw)` /
`restore_reachable(name)` / `classify_pending(name, groupings, allow_forward, ...)`

## 3. 面板页（兼容上游的挂接方式，复刻 ncc_kb 模式）

新增文件/改动点——**全部是新增，不碰上游逻辑**：

- `templates/ncc_community.html`：独立页面，四个 Tab
  1. **群列表**（120 行）：群名 / 分组（多选）/ 允许转发 / 允许发言 / 迎新链接 /
     备注状态（remark_applied + addressing_hit）/ status 徽标（active·pending·
     unreachable）。行内操作：改名、恢复不可达。搜索框过滤。
  2. **分组管理**（16 行）：编号（转发多选菜单用）、forward_enabled 开关。
  3. **拉群关键词**（17+ 行）：关键词 → 目标群（下拉选自群列表），增删改。
  4. **待归类**：discovery 发现的 pending 群，一键归类（选分组+权限）。
- `web_server.py`：约 5 个新路由（`/ncc_community` 页 + get/save 各端点），
  带 @login_required，模式与 /ncc_kb 三路由一致。
- `templates/dashboard.html`：侧栏加一行链接（与 ncc_kb 那行并排）。
- 保存语义与 ncc_kb 相同：读全量→改写→落盘，改完下一条微信消息即生效，无需重启。

## 4. 微信侧指令的去留（减少对话依赖，但不破坏兼容）

| 指令 | 处置 |
|------|------|
| ncc→1 转发流程 | **保留不动**（收集消息天然要在微信里做） |
| 同步 | 去掉 |
| 后台 | 发面板地址 |
| 设拉群 / 迎新设置类 | **保留**，改为直写 registry（和面板同一张表），应急时微信里还能改 |
| 回写notion | 去掉 |
| 检查群组 / 核对备注 / 修备注 / 扫群 | 去掉 |
| 待归类新群播报 | 去掉 |


## 5. 迁移与回滚

- **一次性迁移脚本** `plugins/ncc_community/migrate_notion_off.py`：
  给现有 120 群补 `gid`；合并 remark_overrides → invite_keywords；
  迁移前自动备份 `registry.json.bak-<日期>`。幂等，可重跑。
- **notion_sync.py 保留在库里但完全解除挂接**（import 点全部移除）。
  回滚 = git revert + 跑一次旧「同步」。
- Notion 数据库本身不动、不删，只是不再读写。

## 6. 测试与上线步骤

1. 单测：
   - 改 test_ncc_engine.py 里 parse_notion/upsert 相关用例 → 换成 CRUD 用例；
   - 新增 tests/test_ncc_panel.py（Flask test_client 打新路由，模式抄 test_ncc_kb.py）；
   - mac 上 `PYTHONPATH=. python3 tests/test_ncc_engine.py` 等直接跑文件（CLAUDE.md 5.5）。
2. `python3 -m py_compile` 全部改动文件。
3. 部署：合并进 main → 生产工作树 pull → **清 `plugins/ncc_community/__pycache__`**
   （SMB 直编辑坑，CLAUDE.md 3.6）→ `schtasks /run /tn SWXPanelRestart` → 面板启动机器人。
4. 生产验收（对应 §0 判据 4）：面板改关键词 → 微信实测拉群；面板恢复一个
   unreachable 群 → 下轮群发包含它。

## 7. 风险与已知取舍

- **改名不再自动发现**：Notion 时代改标题即迁移；现在群改名要人在面板点"改名"。
  缓解：🐶备注锁寻址（改名不影响转发），「扫群/检查群组」能发现名实不符。
- **remark_applied 丢掉 Notion 兜底**后，registry.json 若损毁，备注状态只能靠
  「修备注 全部」从微信侧重建（本来也是这么修的，成本可接受）。
- **面板成为单点**：registry.json 纳入每日备份（imports/ 同级 bak 即可，一期先手动）。
- 与"看门狗互杀"修复（转发卡壳那条线）**互不阻塞**，可并行推进；但全量群发的
  真机验收要等互杀修完才有意义。

## 8. 工作量预估

| 阶段 | 内容 | 预估 |
|------|------|------|
| P1 | registry CRUD + gid 迁移脚本 + 单测 | 半天 |
| P2 | 面板页 + 路由 + 前端 | 1 天 |
| P3 | 微信指令改造 + Notion 断开 + 单测修整 | 半天 |
| P4 | 部署 + 生产验收 | 半天（含等低峰验证） |

---

## 9. 实现记录（2026-08-05）

### 落地的文件

| 文件 | 作用 |
|------|------|
| `registry.py` | 加 `gid`、面板 CRUD（见 §2）、`invite_keywords` 结构化 + 旧字符串兼容 |
| `panel.py` | 面板逻辑层（纯 Python，不 import flask / wxbot_core，mac 上可单测） |
| `migrate_notion_off.py` | 一次性迁移，幂等，`--dry` 预览 |
| `templates/ncc_community.html` | 四个 Tab 的独立页 |
| `web_server.py` | 三条薄路由：`/ncc_community`、`/state`、`/action` |
| `templates/dashboard.html` | 侧栏一行链接 |
| `tests/test_ncc_panel.py` | 38 个用例：CRUD / 面板状态 / 操作 / 迁移 |

### 与本 spec 的三处偏离（都是发现问题后的取舍，不是漏做）

1. **§2 写的「合并 `remark_overrides`」是笔误**。`remark_overrides` 是「设备注」用的
   备注字符串覆盖（给超长群名指定短备注），跟拉群关键词无关。实际合并的是
   `config.json` 的 `invite.keywords`（「设拉群」的本地覆盖层），语义与原来
   `invite.py` 的 `keywords.update(icfg[...])` 一致，同名仍以它为准。

2. **§4 说「检查群组 / 核对备注 / 修备注 / 扫群」去掉，实际保留**（只摘掉里面的
   Notion 回写）。理由：这四条都要**驱动微信 UI**，面板做不了，删掉是净损失；
   而且 §7「风险」里自己还写着「『扫群/检查群组』能发现名实不符」，与 §4 冲突。
   判据 3「不再访问 Notion」靠摘掉 `_sync_names_to_notion` 达成，不必删指令。

3. **§4 说「待归类新群播报」去掉，实际保留但改文案**（指向面板而不是 Notion）。
   被拉进新群这件事必须有人知道；去掉的是「请去 Notion 归类」那句话，不是播报本身。

### 顺带做的

- `add_group` / `delete_group`：Notion 时代「在群聊列表加一行 / 删一行」的替代品。
  没有它，一个还没人说过话的新群将**没有任何途径**进入登记表（discovery 只在群里
  有人发言时触发），判据 1 就不成立。
- 分组编号唯一性校验 + 禁用编号 1：重号时 `grouping_name_by_number` 只认第一个，
  另一个永远选不中，属于静默失效。
- 关键词目标群必须在登记表里：拼错群名的关键词是哑弹——用户发了、机器人找不到群，
  只会失败退配额。
- 删群/删分组/改名一律级联（关键词跟着迁移或清掉、分组从所有群身上摘掉）。
- 修好了 4 个**本次改动之前就已失败**的存量用例：`test_apply_remark_idempotent`、
  `test_discovery_new_group`、`test_apply_one_group`、`test_remark_worker_end_to_end`。
  病根是假 `ChatInfo()` 没跟上真机行为——真机只返回
  `{'chat_type','chat_name','group_member_count'}`，**没有 remark 字段**，且群一旦
  有备注，`chat_name` 显示的就是备注本身（CLAUDE.md 3.6 实测钉死）。假对象照这个改完即绿。

### 验证证据

- 单测：15 个文件 388 个用例全绿（`tests/test_ncc_panel.py` 38 个为新增）。
- 迁移：拿**生产 registry.json 副本**跑，120 群补 gid、17 条关键词升级结构化、
  1 条从 config 合并；第二次跑 0/0/0（幂等）。
- 面板：起迷你 HTTP 服务挂 `panel.state()/apply()` + 真实模板，浏览器实操四个 Tab；
  每个操作（增删改群/分组/关键词、归类、改名、恢复）与错误路径逐个跑过，
  改动确认落盘、级联清理干净。

### 部署前的落地体检（2026-08-05，全程只读，没碰生产）

- **改前 vs 改后行为等价**（对上判据 2）：拿**真实生产 registry.json**（120 群 /
  16 分组 / 17 关键词）分别跑 `HEAD~1` 和 `HEAD` 的代码，四项机器人侧输出
  **逐字节一致**——转发目标 105 个、所有群聊寻址串 105 个、分组选择菜单 11 项、
  生效拉群关键词 18 条。
- ★ **新代码吃【未迁移】的老数据完全正常**：没有 gid、关键词还是纯字符串时，
  `invite_map` 走兼容分支、转发/分组/寻址全部照常。
  **所以迁移脚本不是部署的硬前提** —— 万一 ui_watchdog 在迁移前自动重启，
  也不会出事；面板首次打开时 `ensure_gids` 会顺手把 gid 补上并落盘。
  迁移脚本额外做的只有"把 config.json 的 invite.keywords 并进 registry"这件收编工作。
- **生产 Python（3.12.8 / flask 3.1.3）编译通过**：在 win-shukong 上对本 worktree
  跑 `py_compile`，并用 ast 确认三条路由 `/ncc_community`、`/state`、`/action` 注册正常。
- 生产工作树当时在 `main` 且干净（无其它会话的未提交改动）。

### 还没做（§0 判据 4）—— 需要人点头

生产部署会改动共享工作树、并要重启面板进程，按 CLAUDE.md 第 2 节的约定先跟人确认。
步骤：

```bash
ssh win-shukong "cd /d C:\Users\Admin\SiverWXbot_plus-main && git merge --no-ff claude/panel-spec-implementation-748ea5"
```

```bash
ssh win-shukong "cd /d C:\Users\Admin\SiverWXbot_plus-main && python -m plugins.ncc_community.migrate_notion_off --dry"
```

去掉 `--dry` 真跑 → 清 `plugins\ncc_community\__pycache__`（SMB 直编辑坑，CLAUDE.md 3.6）
→ `schtasks /run /tn SWXPanelRestart` → 面板里启动机器人。

验收：打开面板改一个拉群关键词 → 微信私聊发该关键词 → 确认正确拉群；
再把一个 unreachable 群点「恢复可转发」→ 下轮群发包含它。
