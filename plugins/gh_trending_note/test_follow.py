# -*- coding: utf-8 -*-
r"""跟随模式的离线单测：只验证 _should_follow 的判定，完全不碰微信 UI。

在项目根目录跑：  python plugins\gh_trending_note\test_follow.py
"""
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from plugins.gh_trending_note import trigger, config  # noqa: E402

NOW = time.time()
fails = []


_REAL_WINDOW = trigger._in_follow_window


def case(name, *, follow_on, sent_today, ai_at, followed_on, expect):
    config.FOLLOW_AI_NEWS = follow_on
    trigger.already_sent_today = lambda: sent_today
    trigger.ai_news_sent_at = lambda: ai_at
    trigger._followed_on = followed_on
    trigger._in_follow_window = lambda: True   # 时段单独测，这里固定放行
    got = trigger._should_follow()
    ok = (got == expect)
    if not ok:
        fails.append(name)
    print("{} {:<42} 期望={!s:<5} 实际={!s}".format("[OK]  " if ok else "[FAIL]",
                                                   name, expect, got))


TODAY = datetime.date.today()

case("AI 日报今天还没发 → 不跟随",
     follow_on=True, sent_today=False, ai_at=0, followed_on=None, expect=False)

case("AI 日报刚发完 10 秒 → 还不到点",
     follow_on=True, sent_today=False, ai_at=NOW - 10, followed_on=None, expect=True
     if config.FOLLOW_DELAY_SEC <= 10 else False)

case("AI 日报发完 {} 秒 → 该跟随".format(config.FOLLOW_DELAY_SEC + 5),
     follow_on=True, sent_today=False, ai_at=NOW - (config.FOLLOW_DELAY_SEC + 5),
     followed_on=None, expect=True)

case("今天已经跟随过 → 不重入（防每 10 秒疯跑）",
     follow_on=True, sent_today=False, ai_at=NOW - 9999, followed_on=TODAY, expect=False)

case("昨天跟随过、今天没有 → 照常跟随",
     follow_on=True, sent_today=False, ai_at=NOW - 9999,
     followed_on=TODAY - datetime.timedelta(days=1), expect=True)

case("我自己今天已经发过 → 不跟随",
     follow_on=True, sent_today=True, ai_at=NOW - 9999, followed_on=None, expect=False)

case("跟随模式关闭 → 永不跟随",
     follow_on=False, sent_today=False, ai_at=NOW - 9999, followed_on=None, expect=False)

# 时段这条不能走 case()——case 里把 _in_follow_window 固定成放行了，写在那儿必然自相矛盾。
# 换回真实判定单独跑一次，再用下面的边界用例覆盖各时间点。
trigger._in_follow_window = _REAL_WINDOW
config.FOLLOW_AI_NEWS = True
trigger.already_sent_today = lambda: False
trigger.ai_news_sent_at = lambda: NOW - 9999
trigger._followed_on = None
_expect_now = _REAL_WINDOW()
_got_now = trigger._should_follow()
print("[{}] {:<42} 当前时段内={!s:<5} _should_follow={!s}".format(
    "OK  " if _got_now == _expect_now else "FAIL", "真实时段判定与 _should_follow 一致",
    _expect_now, _got_now))
if _got_now != _expect_now:
    fails.append("真实时段判定")

print("-" * 66)
print("时间窗 {} 的边界判定：".format(config.FOLLOW_WINDOW))
for hhmm, want in (("07:59", False), ("08:00", True), ("10:30", True),
                   ("13:00", True), ("13:01", False), ("03:00", False)):
    fake = datetime.datetime.combine(TODAY, datetime.datetime.strptime(hhmm, "%H:%M").time())
    got = _REAL_WINDOW(fake)
    ok = (got == want)
    if not ok:
        fails.append("窗口@" + hhmm)
    print("  {} {} → {!s:<5} (期望 {!s})".format("[OK]  " if ok else "[FAIL]", hhmm, got, want))

print("-" * 66)
print("跟随判定单测：{}".format("全部通过 ✅" if not fails else "失败 {} 项 ❌ {}".format(
    len(fails), fails)))
sys.exit(0 if not fails else 1)
