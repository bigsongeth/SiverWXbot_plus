# AI 协作与维护指南 (AI Collaboration Guide)

本文件旨在指导不同 AI 助手在维护此项目时的协作要点及注意事项，确保项目风格的一致性和功能的稳定性。

## 1. 运行环境与远程操作
*   **远程服务器**：项目运行在远程 Windows 服务器上（别名：`win-shukong`）。
*   **SSH 操作**：所有涉及 `git` 提交、系统服务重启、环境检查的操作，**必须**通过 SSH 连接到 `win-shukong` 执行。
    - 示例：`ssh win-shukong "cd C:\Users\Admin\SiverWXbot_plus-main && git status"`
*   **路径说明**：本地挂载路径（如 `/Volumes/...`）仅用于文件编辑。在服务器端，项目根路径通常为 `C:\Users\Admin\SiverWXbot_plus-main`。

## 2. 自动广告剔除规范
*   **维护定制化**：本项目要求去除所有关于 "DusAPI" 的推广性内容。
*   **同步更新建议**：如果未来从上游仓库拉取了更新，AI 助手应**主动检查并自动剔除** `templates/dashboard.html` 中可能恢复或新增的广告横幅、提示条及推广文案。
*   **功能保留**：剔除的是“广告”，而非“功能”。应保留 DusAPI 作为 SDK 选项，但移除其默认填充地址及所有对比性、引导性的文案。

## 3. API 接口兼容性原则
*   **标准协议优先**：系统核心（`wxbot_core.py`）应优先支持 OpenAI 标准的 `Chat Completions` 协议（`/v1/chat/completions`）。
*   **适配 NewAPI/One-API**：由于项目常搭配 NewAPI 等代理使用，避免使用 OpenAI 较新或非通用的端点（如 `/v1/responses`）。
*   **SDK 降级逻辑**：在 `OpenAI SDK` 调用失败时，应提供基于标准 HTTP 请求的 `_try_responses_api` 备用方案，并确保其解析逻辑与标准协议一致。

## 4. DusAPI 定制化修改与合并冲突处理

> ⚠️ **合并原则：以下区域在合并上游代码时，若发生冲突，一律以我们的版本为准。**

我们对 `wxbot_core.py` 中的 `DusAPI` 类做了三类关键修改，解决了上游版本不支持 reasoning 模型（如 `grok-*-reasoning`）和非 GPT/Claude 模型的问题。上游更新时这些区域极易产生冲突。

### 4.1 模型路由：`else` catch-all（替代硬编码关键词匹配）

**修改位置**：`DusAPI.chat()` 方法中的模型分支判断

*   **上游原版**：只有 `if 'claude' in model` 和 `elif 'gpt' in model` 两个分支，其他模型（grok、gemini 等）全部落入 `else` 报错 → "未识别的模型名称"。
*   **我们的版本**：将 GPT 分支改为 `else` 兜底，删除旧的报错分支。所有非 Claude 的模型统一走 OpenAI 兼容协议（`/v1/chat/completions`）。

```python
# ✅ 我们的版本（正确）
if 'claude' in model.lower():
    ...  # Claude / Anthropic 分支
else:
    ...  # 所有 OpenAI 兼容模型（gpt/grok/gemini/...）

# ❌ 上游版本（会丢模型）
if 'claude' in model.lower():
    ...
elif 'gpt' in model.lower():  # grok 进不来
    ...
else:
    log("未识别的模型名称")  # 白白报错
```

### 4.2 `reasoning_content` 兜底提取

**修改位置**：`_extract_gpt_text()` 静态方法 + `_stream_gpt_text()` 方法

*   **问题背景**：grok-reasoning 等思考模型返回时 `content` 为空字符串，真正的回复在 `reasoning_content` 字段里。
*   **非流式**（`_extract_gpt_text`）：先取 `content`，为空则取 `reasoning_content`。
*   **流式**（`_stream_gpt_text`）：`delta.get('content') or delta.get('reasoning_content')`。

### 4.3 流式 SSE 格式兼容

**修改位置**：`_stream_gpt_text()` 方法

*   **上游原版**：只解析 OpenAI Responses API 格式（`response.output_text.delta`）。
*   **我们的版本**：新增标准 Chat Completions 流式格式解析（`choices[0].delta.content`），兼容绝大多数 API 代理（NewAPI / One-API 等）。

```python
# 我们新增的解析分支：
elif event_type is None and 'choices' in data:
    delta = data['choices'][0].get('delta') or {}
    text = delta.get('content') or delta.get('reasoning_content') or ''
```

### 4.4 OpenAIAPI 类的同步修改

`OpenAIAPI` 类中也做了对应的 `reasoning_content` 兼容：

*   **非流式路径**：`response.choices[0].message` 先取 `content`，为空取 `reasoning_content`。
*   **备用 requests 路径**（`_try_responses_api`）：`msg.get('content')` 为空时取 `msg.get('reasoning_content')`。

### 合并冲突检查清单

从上游拉取代码后，请逐一确认以下内容未被覆盖：

- [ ] `DusAPI.chat()` 中 GPT 分支是 `else:`（不是 `elif 'gpt'`）
- [ ] `DusAPI.chat()` 中没有 "未识别的模型名称" 的 else 报错分支
- [ ] `_extract_gpt_text()` 包含 `reasoning_content` 兜底逻辑
- [ ] `_stream_gpt_text()` 包含 `choices[0].delta` 的 SSE 解析分支
- [ ] `OpenAIAPI.chat()` 非流式路径包含 `reasoning_content` 兜底
- [ ] `OpenAIAPI._try_responses_api()` 包含 `reasoning_content` 兜底

## 5. 代码与 UI 准则
*   **注释与语言**：保持项目现有的中文注释风格。UI 文字必须使用中文，并遵循 Dashboard 的现代简约审美风格。
*   **配置安全性**：修改 `config/config.json` 结构时，需确保 Dashboard 前端（`templates/dashboard.html`）能正确处理默认值，防止由于配置项缺失导致页面渲染失败。
*   **无感热更**：修改逻辑后，若需重启服务生效，应事先告知用户。

## 6. 潜在冲突：主窗口"转发中"闸门（`wxbot_core.py`，★合并上游必看）

**背景**：wxauto 是 UI 自动化，整个微信只有一个主窗口，且有多条线在驱动它：
- **wxautox 监听线程** `message_handle_callback`（群/私聊消息处理、AI 回复、ncc 转发触发）——这是本机器人处理群消息的**主路**；
- **主循环** `ALLListen_mode`(动态新会话轮询) + `Pass_New_Friends`(新好友，切通讯录) + 定时任务；
- `plugins/ncc_community` 的**转发后台线程**。

它们并发会互相把窗口切走 → "转发到一半失败"。且消息处理是同步的（读→知识库/AI 十几秒→回复），
若与转发并发就是灾难。

**用户要求（2026-07）**：进了转发就先把转发做完，其它（加群/加好友/朋友圈/AI回复…）
排队等它做完再按序执行，别互相堵、别丢消息。

**定制方案**：`plugins/ncc_community/wxlock.py` 提供一个全局"转发中"闸门
（`set_forwarding/is_forwarding/wait_while_forwarding`，带超时兜底防卡死）+ `WX_LOCK`。
转发后台线程整个群发期间 `set_forwarding(True)`（`forward.py: _forward_worker` 里 try/finally）。
`wxbot_core.py` 有 **2 处 hook**（都以 `# ncc_community hook` 注释标记）：

1. **主循环** `while self.run_flag:` 内、`_wd_heartbeat()` 之后：转发中就让路（本轮不碰主窗口，
   只心跳+睡；未读消息留在微信里，转发完下一轮自然读到，不丢）：
   ```python
   from plugins.ncc_community.wxlock import is_forwarding as _ncc_is_forwarding  # 循环前导入(带 except 兜底)
   ...
   _wd_heartbeat()
   if _ncc_is_forwarding():
       time.sleep(wait_time)
       continue
   ```
2. **监听回调** `message_handle_callback` 开头：转发中就 `wait_while_forwarding()` 等它做完再处理
   （消息不丢，wxauto 会把后续回调排队，做完按序处理）。

**合并上游后务必确认这 2 处还在**，且 `_forward_worker` 里的 `set_forwarding` try/finally 还在。
主循环因为每轮先 `_wd_heartbeat()` 再判断让路，转发再久也不会触发 ui_watchdog（心跳照常）。

**为什么不再用"细粒度锁交错"**：那样 AI 回复(十几秒)会和转发交错抢窗口；用户明确要"转发做完
再干别的"。故改为闸门让路的**串行排队**模型：转发独占直到完成，其它按序跟上（不并发、不丢消息，
代价是大群发期间收到的消息会延后几分钟处理——已与用户确认接受，选 A）。

## 7. 转发策略：单目标逐群转（★2026-07-09 定型，勿改回多选）

**血的教训**：曾用 `msg.forward([群1, 群2, …])` 传列表 → 微信弹「分别发送给」**多选框**，
逐群勾选。实测转 106 群（其中约 100 个机器人已不在、搜索"无结果"）时，微信**直接未响应
（"Weixin 未响应"）卡死**，用户被迫关掉微信。`OPERATION_WAIT_TIME=1.0` 也救不回来。

**现方案（`plugins/ncc_community/forward.py`）**：`msg.forward(群)` 传**单个字符串** → 走轻量的
「发送给」对话框，一个群一个群转。核心函数：
- `_forward_one_shot(cache_box, bot, source, sig, group, d)`：转**单个**群；stale 才重定位重试，
  其它失败（多为"无结果"=该群没了）**不重试**。
- `_forward_located_message(bot, source, sig, targets, d)`：`for g in targets` 逐个转，每
  `batch_every`(=10) 个群额外歇 `batch_min~batch_max`；"无结果"的群收进 `gone` → `mark_unreachable`。
- `DELAY`：`group_min/max`(群间 2.5-4.5s)、`msg_min/max`(消息间 5-8s)、
  `batch_every/batch_min/batch_max`(每 10 群歇 5-9s)、`max_retries`。
- **保护**：整条一个群都没成功 → 判是"这条消息本身转不了"（视频号等），不冤枉群、不标记不可达。

⚠️ **不要改回传列表/多选框**（用户 2026-07-09 明确"改回一个一个吧"），也**不要**加"大范围确认"
（用户"不用加大范围确认"）。已无 `CHUNK_SIZE`/`group_chunks` 概念。

### 7.1 wxautox4 库内改动（site-packages，★重装/升级必重打）

改过 wxautox4 内一行放慢全局 UI 操作（不在本仓库里，`pip install/upgrade wxautox4` 会被覆盖，
届时需重新改）：

- 文件：`…/site-packages/wxautox4/uia/uiautomation.py`
- 改动：全局 `OPERATION_WAIT_TIME = 0.5` → `OPERATION_WAIT_TIME = 1.0`
  （每次点击/输入后的等待翻倍 → UI 操作更稳；备份在同目录 `uiautomation.py.nccbak`）
- 影响面：全局 UI 操作都变慢约 0.5s（收发消息也稍慢，可接受）。`forward` 本身在
  `msgs/*.pyd`、对话框在 `ui/*.pyd`，都是编译的改不了，只能拧这个底层等待。

## 8. 常见协作任务流程
1.  **文件修改**：通过文件操作工具编辑代码内容。
2.  **验证一致性**：检查是否误引入了已剔除的广告或不兼容的 API 路径。
3.  **合并上游**：拉取上游更新后，按第 4 节清单 + 第 6 节主窗口闸门 hook + 第 7 节库内改动逐项确认。
4.  **远程提交**：通过 SSH 调用 git 命令进行 commit。
5.  **汇报总结**：清晰列出修改点及对应的 commit ID。
