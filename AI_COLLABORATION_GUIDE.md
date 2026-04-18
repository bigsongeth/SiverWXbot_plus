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

## 4. 代码与 UI 准则
*   **注释与语言**：保持项目现有的中文注释风格。UI 文字必须使用中文，并遵循 Dashboard 的现代简约审美风格。
*   **配置安全性**：修改 `config/config.json` 结构时，需确保 Dashboard 前端（`templates/dashboard.html`）能正确处理默认值，防止由于配置项缺失导致页面渲染失败。
*   **无感热更**：修改逻辑后，若需重启服务生效，应事先告知用户。

## 5. 常见协作任务流程
1.  **文件修改**：通过文件操作工具编辑代码内容。
2.  **验证一致性**：检查是否误引入了已剔除的广告或不兼容的 API 路径。
3.  **远程提交**：通过 SSH 调用 git 命令进行 commit。
4.  **汇报总结**：清晰列出修改点及对应的 commit ID。
