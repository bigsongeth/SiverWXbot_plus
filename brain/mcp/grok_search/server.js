#!/usr/bin/env node
// 从 ~/.claude/mcp-servers/grok-search/server.js 原样拷来（2026-09-05），改动请两边同步。
/**
 * grok-search MCP server —— 把 songkey 上两个"自带联网能力"的 chat 模型
 * 包装成两个搜索工具，一个管国外/全球，一个管国内。
 *
 * 背景：songkey(key.bigsong.site) 上绝大多数模型是标准 API 转发，纯模型无工具。
 * 只有两个走逆向网页端链路的模型把网页版的搜索能力顺带过来了：
 *
 *   - grok-chat-fast  → 逆向 grok.com，全球网页 + X(推特) 独家数据  → 工具 web_search
 *   - dots-chat       → 逆向小红书「点点」，小红书站内笔记 + 国内资讯 → 工具 cn_search
 *
 * 为什么做成 MCP 而不是 shell 脚本：Codex 的沙箱默认禁网，脚本里的 curl 会被拦；
 * MCP server 是 Codex 之外的独立进程，不受沙箱限制。这样主 agent 不用开外联权限
 * 也能拿到实时信息。
 *
 * 这两个模型都不能调工具、不能跑代码，所以它们只能是"一次性问答接口"：
 * 进去一个问题，出来一段文本。不要试图让它们当 agent。
 *
 * 零依赖：手写 JSON-RPC over stdio，不装任何 npm 包。
 */

// 兜底搜索。只 require 模块、不读 key 文件（exa.js 里是懒加载），所以 key 清单不在位时
// 这个 MCP 照样起得来，只有真需要兜底那一刻才报错。
const exa = require('./exa.js');

const BASE_URL = process.env.SONGKEY_BASE_URL || 'https://key.bigsong.site/v1';
const API_KEY = process.env.SONGKEY_API_KEY || '';
const MODEL = process.env.SONGKEY_SEARCH_MODEL || 'grok-chat-fast';
const CN_MODEL = process.env.SONGKEY_CN_SEARCH_MODEL || 'dots-chat';
// 60 秒：实测正常返回落在 12–55 秒（非流式 p50 约 29s），60 秒之后基本就是上游 stall 了。
// 从 120 秒下调是因为大脑一轮总预算有限，一次卡死吃掉 120 秒的话这一轮必死（2026-09-08）。
const TIMEOUT_MS = Number(process.env.SONGKEY_TIMEOUT_MS || 60000);
// songkey 通道失败后再打一次（同一模型），两次都失败才换 Exa。上游是间歇性 stall，
// 实测成功与挂死交替出现，重打一次的命中率不低。
const SONGKEY_ATTEMPTS = Number(process.env.SONGKEY_ATTEMPTS || 2);
const EXA_FALLBACK = process.env.EXA_FALLBACK !== '0';

const PROTOCOL_VERSION = '2024-11-05';

// 压幻觉：明确告诉它可以搜，并要求给来源、不确定就直说。
const SYSTEM_PROMPT = [
  '你有联网搜索能力。回答必须基于实时搜索到的内容，不要依赖记忆。',
  '给出关键事实时附上来源链接。',
  '搜不到或无法确认的，直接说"未搜到"，绝对不要编造数字、链接或引用。',
].join('\n');

// 国内版额外压两个实测踩到的坑：
// 1) 它会把上一个交易日的行情当成"今天"（2026-08-29 周六实测，报了当日 A 股收盘涨跌）
// 2) 它擅长的是小红书站内笔记，问站外的东西容易滑回记忆
const CN_SYSTEM_PROMPT = [
  '你有联网搜索能力，可以检索小红书站内笔记和国内公开资讯。回答必须基于实时搜索到的内容，不要依赖记忆。',
  '引用小红书笔记时给出笔记标题和作者；引用资讯时给出来源。',
  '涉及行情、榜单、价格这类有时效的数据，必须写明数据对应的具体日期，不要把上一个交易日/上一期的数据说成"今天"。',
  '搜不到或无法确认的，直接说"未搜到"，绝对不要编造标题、数字、链接或引用。',
].join('\n');

const TOOLS = [
  {
    name: 'web_search',
    description:
      '【国外/全球】联网搜索实时信息并返回文本答案（国际新闻、美股/币价/汇率、英文网页当前内容、海外产品最新发布等）。' +
      '底层是带搜索能力的 Grok，通常会附带来源链接。' +
      '⭐ 最擅长 X（推特）上的社群讨论——X 数据是 xAI 独家的，通用搜索引擎索引不到；' +
      '问"海外大家在聊什么"、"某话题的推文风向"这类问题优先用它。' +
      '⚠️ 查中文互联网内容（小红书笔记、国内本地生活、国内财经/政策、国内舆论口碑）请改用 cn_search，' +
      '本工具对中文社区内容只能靠聚合猜测，实测会编出不存在的笔记标题。' +
      '如果本环境另有通用网页搜索工具，那类工具更适合文档/论文，本工具更适合海外社群舆论和实时行情。' +
      '用自然语言提问即可，可以指定具体网址让它去读。典型耗时 5-20 秒。' +
      '注意：它只返回文本，不能执行代码、不能操作文件。',
    inputSchema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description:
            '要查的问题，自然语言。越具体越好。' +
            '例："现在 BTC 价格多少美元" / "打开 news.ycombinator.com 看第一条标题是什么" / "英伟达最新财报营收"',
        },
      },
      required: ['query'],
    },
  },
  {
    name: 'cn_search',
    description:
      '【国内/中文】联网搜索中文互联网的实时信息并返回文本答案。' +
      '底层是小红书官方 AI「点点」，能直接检索小红书站内笔记，也覆盖国内公开资讯。' +
      '⭐ 最擅长：小红书笔记与种草口碑（会给出真实笔记标题、作者、原文引用）、' +
      '国内本地生活（餐厅/咖啡店/景点/店铺地址营业时间）、国内消费品测评与避雷、' +
      '国内年轻人社群在聊什么；也能查国内新闻、财经政策、A股动态。' +
      '⚠️ 它没有通用网页浏览能力：问国际新闻、美股/币价、打开某个英文网址，请改用 web_search。' +
      '⚠️ 时效数据（行情/榜单/价格）请自己复核日期——实测出现过把上一交易日数据说成"今天"。' +
      '用自然语言提问即可。典型耗时 20-40 秒（比 web_search 慢）。' +
      '注意：它只返回文本，不能执行代码、不能操作文件。',
    inputSchema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description:
            '要查的问题，自然语言，用中文提问。越具体越好。' +
            '例："小红书上最近推荐的杭州咖啡店有哪些，给店名和地址" / ' +
            '"小红书上大家怎么评价某某洗地机，有没有避雷帖" / "国内今天有什么财经新闻"',
        },
      },
      required: ['query'],
    },
  },
];

// 工具名 → 用哪个模型、配哪段 system prompt
const TOOL_ROUTES = {
  web_search: { model: MODEL, system: SYSTEM_PROMPT },
  cn_search: { model: CN_MODEL, system: CN_SYSTEM_PROMPT },
};

// 调试：设 GROK_SEARCH_DEBUG=/path/to/log 可记录收发的每条消息
const DEBUG_LOG = process.env.GROK_SEARCH_DEBUG || '';
function dbg(dir, obj) {
  if (!DEBUG_LOG) return;
  try {
    require('fs').appendFileSync(
      DEBUG_LOG,
      `${new Date().toISOString()} ${dir} ${JSON.stringify(obj).slice(0, 2000)}\n`
    );
  } catch {}
}

function send(msg) {
  dbg('<<', msg);
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function ok(id, result) {
  send({ jsonrpc: '2.0', id, result });
}

function fail(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

async function callSongkey(query, model, systemPrompt) {
  if (!API_KEY) {
    throw new Error('缺少 API key：请设置环境变量 SONGKEY_API_KEY');
  }

  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), TIMEOUT_MS);

  try {
    const resp = await fetch(`${BASE_URL}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${API_KEY}`,
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: query },
        ],
      }),
      signal: ac.signal,
    });

    const raw = await resp.text();

    if (!resp.ok) {
      throw new Error(`上游 HTTP ${resp.status}（模型 ${model}）: ${raw.slice(0, 500)}`);
    }

    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      throw new Error(`上游返回的不是 JSON（模型 ${model}）: ${raw.slice(0, 500)}`);
    }

    const content = data?.choices?.[0]?.message?.content;
    if (!content) {
      throw new Error(`上游返回里没有 content（模型 ${model}）: ${raw.slice(0, 500)}`);
    }
    return content;
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error(`搜索超时（${TIMEOUT_MS}ms，模型 ${model}）`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 三级兜底：songkey 打两次 → 还不行换 Exa。
 *
 * 起因（2026-09-08 排查，证据在 CLAUDE.md 3.22）：`grok-chat-fast` 在 songkey 上只有一个
 * 渠道，上游间歇性 stall，实测 27% 的失败率。以前一次失败就把错误原样交给大脑，大脑只能
 * 自己再搜一次——那要多烧一整轮模型推理，而轮次预算是有限的，通常直接把这一轮拖死。
 *
 * 返回 { text, source }：source 是 'songkey' 或 'exa'。走了 Exa 一定要在文本里说清楚，
 * 否则大脑会把网页索引的结果当成 X（推特）上的一手舆论——那正是 grok 的独家价值所在，
 * Exa 顶不上。让它知道降级了，它才会在回答里把话说软。
 */
async function searchWithFallback(query, route, toolName) {
  const errs = [];
  for (let i = 0; i < Math.max(1, SONGKEY_ATTEMPTS); i++) {
    try {
      return { text: await callSongkey(query, route.model, route.system), source: 'songkey' };
    } catch (err) {
      errs.push(`第 ${i + 1} 次(${route.model}): ${err.message}`);
    }
  }
  if (!EXA_FALLBACK) {
    throw new Error(errs.join('；'));
  }
  let exaText;
  try {
    exaText = await exa.answer(query);
  } catch (err) {
    throw new Error(`${errs.join('；')}；Exa 兜底也失败: ${err.message}`);
  }
  const missing = toolName === 'web_search'
    ? '本次结果来自网页索引，X（推特）上的讨论很可能没覆盖到'
    : '本次结果来自网页索引，小红书站内笔记没覆盖到';
  return {
    text: `【降级提示】${route.model} 连续 ${SONGKEY_ATTEMPTS} 次失败，改用备用搜索 Exa。`
      + `${missing}，回答时请说明信息来源有限、别把它当成一手社群舆论。\n\n${exaText}`,
    source: 'exa',
  };
}

async function handle(msg) {
  const { id, method, params } = msg;

  // 通知（没有 id）不需要回应
  if (id === undefined || id === null) return;

  switch (method) {
    case 'initialize':
      return ok(id, {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: { tools: {} },
        serverInfo: { name: 'grok-search', version: '2.0.0' },
      });

    case 'tools/list':
      return ok(id, { tools: TOOLS });

    case 'tools/call': {
      const name = params?.name;
      const route = TOOL_ROUTES[name];
      if (!route) {
        return fail(id, -32602, `未知工具: ${name}`);
      }
      const query = params?.arguments?.query;
      if (!query || typeof query !== 'string') {
        return ok(id, {
          content: [{ type: 'text', text: '参数错误：需要提供字符串 query' }],
          isError: true,
        });
      }
      try {
        const { text } = await searchWithFallback(query, route, name);
        return ok(id, { content: [{ type: 'text', text }] });
      } catch (err) {
        // 失败必须显式报错，不能静默返回空——否则主 agent 会拿幻觉当结果
        return ok(id, {
          content: [{ type: 'text', text: `搜索失败：${err.message}` }],
          isError: true,
        });
      }
    }

    case 'ping':
      return ok(id, {});

    default:
      return fail(id, -32601, `不支持的方法: ${method}`);
  }
}

let buffer = '';
let pending = 0;      // 在途请求数
let stdinEnded = false;

// stdin 关闭时若还有在途请求，等它们跑完再退出，否则慢查询会被直接砍掉
function maybeExit() {
  if (stdinEnded && pending === 0) process.exit(0);
}

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf('\n')) !== -1) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue; // 不是合法 JSON 就丢掉，别把 server 搞崩
    }
    dbg('>>', msg);
    pending++;
    handle(msg)
      .catch((err) => {
        if (msg?.id !== undefined && msg?.id !== null) {
          fail(msg.id, -32603, String(err?.message || err));
        }
      })
      .finally(() => {
        pending--;
        maybeExit();
      });
  }
});

process.stdin.on('end', () => {
  stdinEnded = true;
  maybeExit();
});
