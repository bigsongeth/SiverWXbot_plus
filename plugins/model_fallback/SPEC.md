# model_fallback —— 模型故障转移

## 要解决的问题

面板里配了多个接口（`api_configs`），但全程只是"选一个用"：`api_index` 指定默认接口，
`group_api_map` / `chat_api_map` 给个别会话指定专属接口。**任何一个挂了就是挂了。**

失败时的实际表现：各 API 类的 `chat()` 内部 `except` 把异常吞掉、返回固定串
`"API返回错误，请稍后再试"`，上层（群聊 `wxbot_core.py:3378`、私聊 `wxbot_core.py:3744`）
看到这个串就替换成 `api_error_reply`——用户看到的"在忙，我稍后回复您"。
上游 500/529/超时/403 在这条链路里全被压成同一个字符串，**状态码根本没往上传**。

## 做法

一个 `FallbackAPI` 包装器，对外暴露与现有 API 类完全一致的 `.chat()` 签名，
内部按顺序试多个真实接口实例，第一个成功就返回。对上层完全透明——历史、人设、
分段、图片识别、接话闸门全部复用，用户体感只是"这次回复慢了两秒"，问题不用重发。

### 失败判定（第一版不分错误码）

2026-08-03 定：不区分 500 / 403 / 529，**任何失败都切下一个**。理由是底层把状态码
吞了，要精确分流就得改四个 API 类的 7 个 return 点，合并上游的冲突面变大，先不值当。
代价是 403（key 废了）这类不该重试的错误也会白试一次备用接口，浪费一次调用但不影响结果。

判定为失败的三种情况：
1. `chat()` 抛异常；
2. 返回值等于 `"API返回错误，请稍后再试"`（底层吞异常后的固定串）；
3. 返回值为 None 或空白串。

### 配置（全局一条链，所有会话共用）

存在 `config/config.json`，跟 `api_configs` 同一份，由现有的"API 接口配置"面板一起保存：

| 字段 | 类型 | 含义 |
|------|------|------|
| `fallback_switch` | bool | 总开关，默认 `false` |
| `fallback_chain` | list[int] | `api_configs` 的索引，**有序**，从前往后试 |

索引由前端在保存时与 `api_configs` **同一次遍历**生成，所以增删接口不会错位。
手改 `config.json` 时要自己保证索引有效（越界项会被静默跳过并记日志）。

### 不重复试同一个接口

会话原本用的接口可能已经在链里（比如默认接口就是链上第一个）。按
`(base_url, 模型名, api_key 前 8 位)` 做身份去重，同一个身份在一次调用里只试一次。

### 与 ncc_kb 的关系

`_get_group_api` / `_get_chat_api` 的所有分支（含知识库接口）都会被包一层。
知识库端点挂了会退到 `fallback_chain` 上的普通模型——回答里没有知识库内容，
但比"在忙，我稍后回复您"强。

## hook 位置（合并上游后逐个确认）

`wxbot_core.py` 两处，做法是**原逻辑整体挪进 `_resolve_*_api`，外层新函数包一层**，
这样上游若改接口选择逻辑，冲突落在函数体内部、diff 上下文一致，merge 大概率自动过：

```python
def _get_group_api(self, group_name):
    return self._with_fallback(self._resolve_group_api(group_name))

def _resolve_group_api(self, group_name):
    <上游原逻辑 + ncc_kb hook，一行没动>
```

`_get_chat_api` 同理。`_with_fallback` 就定义在这两个函数上面。

## 生效时机

配置在 `bot.config` 加载时读入，和 `api_configs` 一样——**改完要重启机器人线程**
（面板 `/stop_bot` + `/start_bot`），不是改完下一条消息就生效。

## 单测

```bash
cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_model_fallback.py
```

`chain.py` 刻意不 import `wxbot_core`（那会拉起 wxautox 依赖），所以 mac 上能裸跑。
