# -*- coding: utf-8 -*-
"""reply_shape：管分段回复的"形状"，别让没信息量的话单独占一个气泡。

起因（2026-08-03）：群里问 DeepSeek 新模型的评价，肥肉回了 6 条（顶满上限），
其中只有 3 条有内容：

    1. 肥肉在键盘上打了个盹，醒来就刷到了 DeepSeek-V4-Flash-0731 这波新模型。   ← 铺垫
    2. X上大家推得飞起：284B参数 MoE，只激活13B，成本低到离谱……                ← 有效
    3. 但也有小吐槽——推理清晰度不如大模型，指令遵循有时要自己补救。            ← 有效
    4. 总的来说，性价比炸裂，适合 Agent 和本地部署。                            ← 有效
    5. 想看最新推文？肥肉给您扫了。                                             ← 废话
    6. 需要我继续挖？发个信号就行。                                             ← 废话

上游的 SPLIT_PROMPT_TEMPLATE 只要求"模仿真人拆分多条"，对每条的信息量没有任何约束，
模型就会拿开场白和收尾邀请去凑条数。刷屏且信息密度低。

两层处理，都不改上游那个模板（它是模块级常量，改了合并上游必冲突）：
- `augment_split_prompt(prompt)`：在上游格式要求后面补一句"每条都得自带实质信息"。
  治本，但模型不一定每次都听。
- `merge_thin_parts(parts, max_chars)`：发送前把过短的碎片并进相邻条。确定性兜底，
  接住模型没听话的情况。

hook 2 处，都在 wxbot_core.py：`_build_split_prompt` 与 `_parse_split_reply`。
"""
from __future__ import annotations

import re

from . import store

# 代码块（``` 围栏 / ` 行内）整段保护起来，里面的符号一个都不许动。
_CODE_RE = re.compile(r'(```.*?```|`[^`\n]+`)', re.S)
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*', re.S)               # **加粗**
_HEADING_RE = re.compile(r'^[ \t]{0,3}#{1,6}[ \t]+', re.M)   # # 标题
# 分隔线：一整行只有同一个符号重复三次以上。`^-\s` 的列表项不会命中（只有一个 -）。
_HR_RE = re.compile(r'^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$', re.M)
_BLANKS_RE = re.compile(r'\n{3,}')

EXTRA_RULE = """
【每条都要有信息量】
拆出来的每一条都必须自带实质内容。下面这些不要单独占一条：
- 开场铺垫："我看看啊""让我想想""刚刷到这个消息"
- 收尾邀请："还要我继续吗""需要的话告诉我""想看更多就说一声"
- 纯过渡："另外""总之""顺便说一下"
要么把它并进相邻那条一起说，要么干脆不写。
**宁可少拆几条，也不要发出没有信息量的气泡。** 对方看到的是一串聊天泡泡，
每多一条没内容的，就多一次打扰。
"""


def augment_split_prompt(prompt: str) -> str:
    """在上游的分段格式要求后面补一条"每条都要有信息量"的约束。"""
    if not store.enabled() or not prompt:
        return prompt
    rule = store.load().get("extra_rule") or EXTRA_RULE
    # 上游模板把角色设定放在最后，我们的约束插在角色设定之前，跟格式要求待在一起
    marker = "【以下是你的角色设定】"
    if marker in prompt:
        head, _, tail = prompt.partition(marker)
        return head + rule + "\n" + marker + tail
    return prompt + "\n" + rule


def merge_thin_parts(parts, max_chars):
    """把过短的碎片并进相邻条，返回新的分条列表。

    只按长度判断——"有没有信息量"是语义问题，交给 prompt 那层去说；这里只兜底
    最明显的情况（十来个字的收尾邀请、开场白）。合并后超过 max_chars 就不合，
    免得把上游的单条长度约束撑破。
    """
    if not store.enabled() or not parts or len(parts) <= 1:
        return parts
    min_chars = int(store.load().get("min_chars", 20) or 20)
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 150

    out = []
    for p in parts:
        if out and len(p) < min_chars and len(out[-1]) + len(p) + 1 <= limit:
            out[-1] = out[-1] + "\n" + p          # 短碎片并进上一条
        else:
            out.append(p)
    # 首条太短的情况上面那轮碰不到（那时 out 还是空的），单独往后并一次
    if len(out) > 1 and len(out[0]) < min_chars and len(out[0]) + len(out[1]) + 1 <= limit:
        out[1] = out[0] + "\n" + out[1]
        out.pop(0)
    return out


def _strip_md_segment(seg: str) -> str:
    seg = _BOLD_RE.sub(r'\1', seg)
    seg = _HEADING_RE.sub('', seg)
    seg = _HR_RE.sub('', seg)
    return seg


def strip_markdown(text: str) -> str:
    """剥掉微信渲染不了的 Markdown 标记。

    微信是纯文本的，模型写的 `**大理**` 会原样显示成一堆星号，看着像乱码。
    人设里写了"不要用 Markdown"也管不住——实测散文式回答很干净，一到分类罗列
    （据点清单那种）就破戒，这是模型组织结构化信息时的本能。所以要在发送前兜一道。

    只剥三样：**加粗**、# 标题、--- 分隔线。
    留着不动的：
    - 行首的 `- ` 列表符号——当纯文本读也清楚，剥了反而分不清层次；
    - 代码块——肥肉会讲技术，围栏剥了代码就糊成一坨，块内的符号也一律不碰
      （`__init__` 这种别被当成 Markdown 处理掉）。
    """
    if not store.enabled() or not text:
        return text
    parts = _CODE_RE.split(text)
    # split 带捕获组：偶数下标是普通文本，奇数下标是被保护的代码块
    out = [seg if i % 2 else _strip_md_segment(seg) for i, seg in enumerate(parts)]
    return _BLANKS_RE.sub('\n\n', ''.join(out)).strip()


def get_config() -> dict:
    return store.load()


def save_config(cfg: dict) -> None:
    store.save(cfg)
