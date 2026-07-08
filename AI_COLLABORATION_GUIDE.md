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

## 6. 潜在冲突：主窗口串行锁（`wxbot_core.py` 主循环，★合并上游必看）

**背景**：wxauto 是 UI 自动化，整个微信只有一个主窗口。机器人主循环（单线程）会跑
消息轮询 `ALLListen_mode`(内部 `GetNextNewMessage`) 和新好友检查 `Pass_New_Friends`
(内部 `SwitchToContact` 切通讯录)，而 `plugins/ncc_community` 的**转发跑在后台线程**也要
驱动主窗口。两者并发会互相把窗口切走 → "转发到一半失败"。

**定制方案**：用一把共享锁 `plugins/ncc_community/wxlock.py: WX_LOCK`（RLock）把三方串行化。
`wxbot_core.py` 的 `run()` 主循环里有 **3 处 hook**（都以 `# ncc_community hook` 注释标记）：

1. 主循环 `while self.run_flag:` 之前，导入锁：
   ```python
   try:
       from plugins.ncc_community.wxlock import WX_LOCK as _NCC_WX_LOCK
   except Exception:
       import threading as _ncc_th
       _NCC_WX_LOCK = _ncc_th.RLock()
   ```
2. **新好友检查**改为"抢不到锁就让路"（转发优先，抢不到不重置计数、下轮再来）：
   ```python
   if _NCC_WX_LOCK.acquire(blocking=False):
       try:
           self.Pass_New_Friends()
       except Exception as e:
           self.is_err(...)
       finally:
           _NCC_WX_LOCK.release()
           check_new_counter = 0
   # 没抢到锁：跳过，不重置计数
   ```
3. **消息轮询** `ALLListen_mode` 包在 `with _NCC_WX_LOCK:` 内。

**合并上游后务必确认这 3 处还在**。转发线程侧对应地在 `plugins/ncc_community/forward.py`
用 `MAIN_WINDOW_LOCK`（= 同一个 `WX_LOCK`）包裹每次 `roll_into_view + forward` 与
读取/汇报操作。锁是 RLock：同线程可重入，跨线程互斥。

**优先级原则**：新好友检查最低（抢不到就跳过）；消息轮询正常参与排队（保证还能收指令）；
转发按操作粒度持锁（每次 forward 一批群后释放，让轮询插空）。

## 7. 常见协作任务流程
1.  **文件修改**：通过文件操作工具编辑代码内容。
2.  **验证一致性**：检查是否误引入了已剔除的广告或不兼容的 API 路径。
3.  **合并上游**：拉取上游更新后，按第 4 节清单 + 第 6 节主窗口锁 hook 逐项确认定制未被覆盖。
4.  **远程提交**：通过 SSH 调用 git 命令进行 commit。
5.  **汇报总结**：清晰列出修改点及对应的 commit ID。
