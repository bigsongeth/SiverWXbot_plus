// Exa 兜底搜索 —— 只在 songkey 的 grok/dots 通道连续失败之后才用。
//
// 为什么需要它（2026-09-09 实测）：`grok-chat-fast` 在 songkey 上只有一个渠道（外部公益站
// freeapi.dgbmc.top），间歇性 stall，我自己连打 26 次有 7 次失败（27%）。失败形态有三种：
//   ① 非流式撞 nginx proxy_read_timeout 300s，报 504；
//   ② 流式逃过读超时，返回 HTTP 200 + 一个内容帧都没有的空 SSE（还照常扣费）；
//   ③ 上游直接断连（SSL EOF）。
// new-api 这一层不设超时、RetryTimes=5 也无处可切（没有第二个渠道），所以只能在客户端换一家。
//
// 刻意不复用 ~/.claude/mcp-servers/exa-pool/server.js 那套 KeyPool：那份要 import dsh 插件
// 的 lib（`~/.dsh/plugins/dsh-web-search-exa-pool/`），而大脑跑在 mac-mini 上，为了一个兜底
// 把整个插件目录搬过去不划算。这里只读同一份 key 清单（纯字符串数组），失败就换下一把，
// park 状态不共享——代价是两边各自试错，可以接受。
//
// 零依赖，CommonJS（和 server.js 保持一致）。

const fs = require('fs');
const os = require('os');
const path = require('path');

const EXA_BASE = process.env.EXA_BASE_URL || 'https://api.exa.ai';
// ★ 必须用 userInfo() 而不是 os.homedir()：homedir() 读的是 $HOME，而大脑的 run.py 把
// HOME 指到了 ~/feirou-brain-data（为的是不让 dsh 扫到宿主 ~/.agents/skills）。用 homedir()
// 就会去 ~/feirou-brain-data/.dsh/ 找 key，永远找不到，兜底 100% 静默失效 ——
// 2026-09-09 首次上线就踩了，端到端跑出来才发现。userInfo() 读系统 passwd，不受 HOME 影响。
const HOME = process.env.EXA_HOME || os.userInfo().homedir;
const KEYS_FILE = process.env.EXA_KEYS_FILE || path.join(HOME, '.dsh', 'exa-keys.json');
// 只读：dsh 那个 exa-pool 插件探出来的坏 key 清单，格式 {key: {reason, detail, until}}。
// 拿它当先验，能省掉一轮盲试。我们绝不写它，免得和插件抢文件。
const POOL_STATE_FILE = process.env.EXA_POOL_STATE_FILE || path.join(HOME, '.dsh', 'exa-pool-state.json');
// 可写：本模块自己踩到的坏 key 记在这儿。MCP 进程虽然常驻，但 dsh 重启就重来，
// 不落盘的话每次重启后的第一次兜底都要盲试一轮 —— 而兜底往往只有这一次机会。
const PARK_FILE = process.env.EXA_PARK_FILE || path.join(HOME, '.dsh', 'exa-fallback-park.json');
// 20 秒：实测 26 次 /answer 全部在 1.4–4.5 秒返回（p90 3.8s），没有一次超过 5 秒。
// 给到 20 秒纯粹是天花板，正常永远碰不到。
const TIMEOUT_MS = Number(process.env.EXA_TIMEOUT_MS || 20000);
// 10 把：park 先验已经排掉大部分坏 key，剩下的基本可用；坏 key 是立刻返回 401/402 的，
// 多试几把几乎不花时间，但能兜住「先验过期、池子又废了一批」的情况。
const MAX_KEYS = Number(process.env.EXA_MAX_KEYS || 10);

let _keys = null;
let _lastGood = null;   // 进程内记住上次成功的 key，下次先用它，省得每轮从头试

function loadKeys() {
  if (_keys) return _keys;
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(KEYS_FILE, 'utf8'));
  } catch (err) {
    throw new Error(`读不到 Exa key 清单 ${KEYS_FILE}：${err.message}`);
  }
  const list = Array.isArray(raw) ? raw : Array.isArray(raw?.keys) ? raw.keys : [];
  _keys = list.filter((k) => typeof k === 'string' && k.length > 8);
  if (_keys.length === 0) throw new Error(`Exa key 清单是空的：${KEYS_FILE}`);
  return _keys;
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

let _park = null;   // {key: untilMs}，两份文件合并后的结果

function loadPark() {
  if (_park) return _park;
  _park = {};
  const now = Date.now();
  for (const file of [POOL_STATE_FILE, PARK_FILE]) {
    const d = readJson(file);
    if (!d || typeof d !== 'object') continue;
    for (const [k, v] of Object.entries(d)) {
      const until = typeof v === 'number' ? v : Number(v?.until || 0);
      if (until > now) _park[k] = Math.max(_park[k] || 0, until);
    }
  }
  return _park;
}

// 402 是当月额度用尽（Exa 每月 1 号回血），401/403 基本是永久损耗，429 只是一时限流。
function parkUntil(status) {
  const now = new Date();
  if (status === 402) return Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1);
  if (status === 429) return Date.now() + 10 * 60 * 1000;
  return Date.now() + 365 * 24 * 3600 * 1000;
}

function park(key, status) {
  loadPark()[key] = parkUntil(status);
  const d = readJson(PARK_FILE) || {};
  d[key] = { reason: `http ${status}`, until: parkUntil(status) };
  try {
    fs.writeFileSync(PARK_FILE, JSON.stringify(d), { mode: 0o600 });
  } catch {
    // 写不进去就只在内存里记着，别让兜底本身崩掉
  }
}

// 先排掉已知的坏 key，再把上次成功的排头，其余打乱 —— 不打乱的话池子前几把会被打爆，
// 后面 90 多把一直闲着。★ 排掉这一步是必须的：2026-09-09 实测 102 把里 74 把已废
// （64 个 402 等下月回血、10 个被拒），不排的话随机抽 6 把有 16% 概率全是坏的 ——
// 我第一次自测就连踩两次，兜底直接失效。
function candidates() {
  const parked = loadPark();
  const now = Date.now();
  const all = loadKeys().filter((k) => !(parked[k] > now));
  const pool = all.length ? all : loadKeys();   // 全被 park 了就死马当活马医，总比不试强
  const rest = pool.filter((k) => k !== _lastGood);
  for (let i = rest.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  return (_lastGood && pool.includes(_lastGood) ? [_lastGood] : []).concat(rest).slice(0, MAX_KEYS);
}

/** 402=当月额度用尽，401/403=key 废了，429=限流；这几种换把 key 就行，别当成 Exa 挂了。 */
function keyIsDead(status) {
  return status === 401 || status === 402 || status === 403 || status === 429;
}

async function callExa(pathname, body) {
  const failures = [];
  for (const key of candidates()) {
    let res;
    try {
      res = await fetch(`${EXA_BASE.replace(/\/$/, '')}${pathname}`, {
        method: 'POST',
        headers: { 'x-api-key': key, 'content-type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch (err) {
      failures.push(`${key.slice(0, 8)}: ${err.name}`);
      continue;
    }
    if (res.ok) {
      _lastGood = key;
      return res.json();
    }
    const detail = await res.text().catch(() => '');
    failures.push(`${key.slice(0, 8)}: ${res.status}`);
    if (keyIsDead(res.status)) park(key, res.status);
    if (!keyIsDead(res.status)) {
      // 不是 key 的问题（500/503 之类），换 key 也没用，直接认输
      throw new Error(`Exa 上游 HTTP ${res.status}: ${detail.slice(0, 200)}`);
    }
  }
  throw new Error(`Exa 试过的 ${failures.length} 把 key 全失败 —— ${failures.join('; ')}`);
}

// query 里带网址时要走另一条路，见 answer() 的注释。裸域名（news.ycombinator.com）也算，
// 因为 web_search 的工具描述就是这么教模型提问的。
const URL_RE = /\bhttps?:\/\/[^\s，。、）)"'』」]+|\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|ai|cn|co|dev|app|news|xyz)(?:\/[^\s，。、）)"'』」]*)?/gi;

function extractUrls(query) {
  const hits = String(query || '').match(URL_RE) || [];
  const out = [];
  for (const h of hits) {
    const u = /^https?:\/\//i.test(h) ? h : `https://${h}`;
    if (!out.includes(u)) out.push(u);
    if (out.length >= 3) break;
  }
  return out;
}

/** 现爬指定网页的正文。Exa 的 /answer 做不到这件事（实测只会翻出几周前的历史快照）。 */
async function readPages(urls) {
  const data = await callExa('/contents', {
    urls,
    text: { maxCharacters: 4000 },
    livecrawl: 'always',    // 少了它就是吃索引里的旧快照，问「现在首页第一条是什么」必答错
  });
  const parts = (data.results || []).map((r) => {
    const body = String(r.text || '').trim() || '(抽取不到正文)';
    return `## ${r.title || r.url}\n${r.url}\n\n${body}`;
  });
  if (parts.length === 0) throw new Error('Exa 没抓到任何页面正文');
  const failed = (data.statuses || []).filter((s) => s.status !== 'success');
  return parts.join('\n\n---\n\n')
    + (failed.length ? `\n\n（另有 ${failed.length} 个网址没抓下来）` : '');
}

/**
 * 拿一个自然语言问题换一段可直接交给大脑的文本。
 * 形态刻意和 songkey 那两个搜索工具一致：进去一个问题，出来一段带来源的文字。
 *
 * 两条路（2026-09-09 实测定的）：
 * - 问题里带网址 → `/contents` + `livecrawl:'always'` 现爬正文。实测问「打开
 *   news.ycombinator.com 看第一条标题」时 `/answer` 会答"无法确定"并引用 8 月的旧页面，
 *   换 `/contents` 则 5.5 秒抓到当前首条，和 grok 的答案逐字一致。
 * - 其余 → `/answer`（p50 2.3s、$0.005，26/26 成功）。
 *
 * ⚠️ 顶不上 grok 的地方：X（推特）。Exa 索引里没有 x.com —— `category:"tweet"` 被官方下架
 * （HTTP 400），`includeDomains:["x.com"]` 返回 0 条，直接读推文 URL 是 403。它给的是
 * **媒体转述的 X**，而且实测会拿账号主页链接去挂靠引语，看着像一手其实无法核实。
 * 所以 server.js 里那句降级提示必须留着。
 */
async function answer(query) {
  const urls = extractUrls(query);
  if (urls.length) return readPages(urls);
  const data = await callExa('/answer', { query, text: false });
  const text = String(data?.answer || '').trim();
  if (!text) throw new Error('Exa 返回里没有 answer');
  const cites = (data.citations || [])
    .map((c, i) => `[${i + 1}] ${c.title || '(无标题)'}\n    ${c.url}`)
    .join('\n');
  return `${text}\n\n来源：\n${cites || '（未给出来源）'}`;
}

module.exports = { answer, extractUrls };
