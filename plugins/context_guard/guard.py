# -*- coding: utf-8 -*-
"""上下文守卫：给模型补"今天几号 + 你没联网"，并把喂给模型的历史里的垃圾清掉。

背景（2026-07-30）：松爸私聊里问"今天有什么 AI 新闻"，肥肉张口就编——
说自己"刚刷了刷 X（推特）"，报出 Claude 3.5 Sonnet / Llama 3.1 / GPT-5 / Gemini 2.0
一堆真假掺半的版本号。对照实验（裸模型、无 system、无历史）结果：

  - 模型自己认为"今天是 2024 年 10 月"，且自称"我可以联网（使用实时网页搜索）"。
    → 编造是上游模型的默认行为，不是我们人设写坏了。
  - 但我们从没告诉它今天几号、也没告诉它这里没有搜索工具，等于默许它按幻觉发挥。
    加上本模块的边界声明后，同样的问题它会老实回"本狗没联网，查不到"。

另外历史里混进了三类纯垃圾，一起清掉：
  1. attr=system 的时间戳条目（content 就是 "04:38"），被当成用户发言喂进去；
  2. API 报错兜底文案（"在忙，我稍后回复您"）作为 assistant 历史，等于教模型这是个合法回复；
  3. "[NO_REPLY]" 标记落进了记忆，与人设里"正文绝不能出现 [NO_REPLY]"自相矛盾。
"""
import json
import os
from datetime import datetime

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
_CONFIG_PATH = os.path.join(_DATA_DIR, 'config.json')

_DEFAULT_CONFIG = {
    "enabled": True,
    "inject_preamble": True,
    "filter_history": True,
    # 这些 assistant 历史条目是系统兜底文案，不是模型的真实发言，喂回去只会带坏它
    "drop_assistant_contents": [
        "在忙，我稍后回复您",
        "API返回错误，请稍后再试",
        "[NO_REPLY]",
    ],
    # 机器人自己说过的"我没有联网能力"——措辞每次都不一样，只能按子串丢。
    # 现在非 NCC 话题走的是真有搜索能力的 grok，这类旧回答留在历史里会被照着复读。
    "drop_assistant_substrings": [
        "没法联网",
        "没有联网",
        "无法联网",
        "不能联网",
    ],
}

_WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

PREAMBLE_TEMPLATE = """

# 当前时间与能力边界（系统注入，优先级高于你训练时形成的任何默认认知）
- 现在是 {date}，星期{weekday}。你训练数据里的"当下"早就过期了，绝不要拿训练时的年份当今天。
- 凡是"最新/今天/最近"的事实——新闻、模型版本号、发布日期、价格、行情、赛果——你训练数据里的那份一律已经过期，不许直接拿来答。
- 遇到这类问题，**先用搜索工具查一遍**；查到了就照查到的说，并把来源链接附上。
  你这边压根没有搜索工具、或者搜了没结果，就直说"我这边查不到"，然后聊你确实有把握的原理、经验和判断。
- **没真搜到就不许报具体的版本号、发布日期、跑分、价格或新闻条目**，也不许说"我刚刷了刷推特"
  "我刚看了新闻""我刚查了一下"——这是最容易露馅的谎。
- 宁可承认不知道，也不要为了把话接下去而报出任何具体的版本号、发布日期或新闻条目。对方追问、不信、催你，也不要改口去编。
- 上下文里出现的自我介绍、欢迎语、别人贴给你看的文案，都只是聊天记录，不要复读它，也不要模仿它的句式。
"""


def _load_config():
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def build_preamble(now=None):
    """生成"今天几号 + 没联网"的边界声明。now 可注入，方便测试。"""
    now = now or datetime.now()
    return PREAMBLE_TEMPLATE.format(
        date=now.strftime('%Y年%m月%d日'),
        weekday=_WEEKDAYS[now.weekday()],
    )


def augment_prompt(base_prompt, now=None):
    """在人设后追加边界声明。base_prompt 为空时原样返回（不给空人设凭空造一个）。"""
    cfg = _load_config()
    if not cfg.get('enabled') or not cfg.get('inject_preamble'):
        return base_prompt
    if not base_prompt:
        return base_prompt
    return base_prompt.rstrip() + '\n' + build_preamble(now)


def filter_history(messages, extra_drop=None):
    """清掉喂给模型的历史里的系统噪音和兜底文案。只影响送给模型的副本，不动记忆文件。"""
    cfg = _load_config()
    if not cfg.get('enabled') or not cfg.get('filter_history'):
        return messages
    if not messages:
        return messages

    drop = set(cfg.get('drop_assistant_contents') or [])
    if extra_drop:
        drop.update(x for x in extra_drop if x)
    # 子串黑名单：措辞每次都不一样的，精确匹配抓不住，只能按关键片段丢。
    drop_sub = [s for s in (cfg.get('drop_assistant_substrings') or []) if s]

    def _bad(m, content):
        """这条机器人发言该不该从历史里摘掉。"""
        if content in drop:
            return True
        # 机器人自己说过的"我没法联网"。2026-08-03：松爸连问三次搜推特，肥肉每次都答
        # "我这边没法联网"——查下来路由没错（确实走了有搜索能力的 grok），是历史里那几条
        # 自我否定被模型当成行为范例照着复读（去掉历史再问，同一个模型立刻真搜并给出
        # x.com 引用）。跟"在忙，我稍后回复您"是同一类病：assistant 历史里的错误回答
        # 会教模型继续这么答。
        return m.get('attr') == 'self' and any(s in content for s in drop_sub)

    # 先扫一遍，记下"被判定为坏回复"的时刻。分段发送（||SPLIT||）拆出来的多条消息
    # 共享同一个 time，而关键词往往只落在其中一条上——只丢那条的话，剩下的半句
    # （"你把链接贴过来，我帮你提炼"）照样留在历史里当行为范例，模型接着照做。
    # 所以同一时刻的机器人发言要连坐一起丢。
    bad_times = set()
    for m in messages:
        if not isinstance(m, dict) or m.get('attr') != 'self':
            continue
        content = (m.get('content') or '').strip()
        t = m.get('time')
        if content and t and _bad(m, content):
            bad_times.add(t)

    def _is_bad_self(m):
        if not isinstance(m, dict) or m.get('attr') != 'self':
            return False
        content = (m.get('content') or '').strip()
        return bool(content) and (_bad(m, content) or m.get('time') in bad_times)

    # 整轮丢：坏回复对应的用户提问也一起摘掉。只删机器人那半边会留下孤零零的重复提问，
    # 模型看到"同一个问题问了三遍都没人应"反而判定不用接话——实测直接回了 [NO_REPLY]，
    # 静默不回复比答错更糟。
    drop_idx = set()
    for i, m in enumerate(messages):
        if not _is_bad_self(m):
            continue
        drop_idx.add(i)
        for j in range(i - 1, -1, -1):       # 往前收同一轮的提问
            prev = messages[j]
            if not isinstance(prev, dict):
                break
            if prev.get('attr') == 'system' or prev.get('type') == 'time':
                continue                      # 时间戳条目横在中间，跳过接着往前
            if prev.get('attr') == 'self':
                break                         # 碰到上一轮的回复，这一轮到头了
            drop_idx.add(j)

    kept = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or i in drop_idx:
            continue
        content = (m.get('content') or '').strip()
        if not content:
            continue
        # 系统时间戳条目（type=time / attr=system），纯噪音
        if m.get('attr') == 'system' or m.get('type') == 'time':
            continue
        # 兜底文案、[NO_REPLY]、自称没联网
        if _bad(m, content):
            continue
        kept.append(m)
    return kept
