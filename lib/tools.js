/**
 * dsh-kb —— P4：把知识库的检索/详情/问答暴露成 DSH 工具，供 agent 直接调用。
 *
 * 设计要点：
 *  - 工具只走本机回环（127.0.0.1:8816），不经过 DSH 的浏览器鉴权；
 *    agent 本身已由 DSH 会话授权，这里不再做第二套认证。
 *  - 输出统一 { schema, render }：schema 给模型/类型渲染，render 给对话里看。
 *  - 每个工具都设超时；问答（RAG）会调模型，给到 180s。
 *  - 后端未启动时抛出带说明的错误，而不是返回空结果，agent 能据此提示用户。
 */

const UPSTREAM = `http://127.0.0.1:${Number(process.env.DSH_KB_UPSTREAM_PORT || 8816)}`

async function kbJson(path, { method = 'GET', body, timeoutMs = 30_000 } = {}) {
  const response = await fetch(UPSTREAM + path, {
    method,
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!response.ok) {
    let detail = ''
    try {
      detail = (await response.text()).slice(0, 200)
    } catch {
      /* 忽略读取失败 */
    }
    throw new Error(`知识库接口 ${path} 返回 HTTP ${response.status}${detail ? `：${detail}` : ''}`)
  }
  return response.json()
}

const ITEM_PROPERTIES = {
  title: { type: 'string' },
  path: { type: 'string' },
  url: { type: 'string' },
  date: { type: 'string' },
  category: { type: 'string' },
  series: { type: 'string' },
  summary: { type: 'string' },
  snippet: { type: 'string' },
  score: { type: 'number' },
}

function textResult(text) {
  return [{ type: 'text', text }]
}

function formatItems(items) {
  if (!items || !items.length) return '知识库里没有检索到相关报告。'
  return items
    .map((item, index) => {
      const parts = [`${index + 1}. ${item.title}`]
      const meta = [item.date, item.category, item.series].filter(Boolean).join(' · ')
      if (meta) parts.push(`   ${meta}`)
      if (item.summary) parts.push(`   ${item.summary.slice(0, 160)}`)
      if (typeof item.score === 'number') parts.push(`   相关度 ${item.score}`)
      parts.push(`   路径：${item.path}`)
      return parts.join('\n')
    })
    .join('\n\n')
}

export function registerKbTools(ctx) {
  const disposers = []

  disposers.push(
    ctx.tools.register({
      name: 'kb_search',
      description:
        'Search the personal knowledge base (reports written by Hermes) with hybrid semantic + keyword ranking. ' +
        'Use it before answering questions about the user\'s own reports, market notes, article digests, or Minecraft version notes.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          q: { type: 'string', description: 'Search query in Chinese or English.' },
          category: { type: 'string', description: 'Optional exact category filter, e.g. "市场全景".' },
          limit: { type: 'integer', description: 'Max results, default 6, at most 20.' },
        },
        required: ['q'],
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            total: { type: 'integer' },
            mode: { type: 'string' },
            items: { type: 'array', items: { type: 'object', additionalProperties: false, properties: ITEM_PROPERTIES } },
          },
          required: ['total', 'mode', 'items'],
        },
        render: (_args, value) =>
          textResult(`检索模式：${value.mode}，命中 ${value.total} 篇\n\n${formatItems(value.items)}`),
      },
      timeoutMs: 60_000,
      async execute(args) {
        const limit = Math.max(1, Math.min(20, Number(args.limit) || 6))
        const params = new URLSearchParams({ q: String(args.q || ''), mode: 'hybrid', limit: String(limit) })
        if (args.category) params.set('category', String(args.category))
        const data = await kbJson(`/api/kb/search?${params.toString()}`)
        return {
          total: data.total || 0,
          mode: data.mode || 'lexical',
          items: (data.items || []).map((item) => ({
            title: item.title || '',
            path: item.path || '',
            url: item.url || '',
            date: item.date || '',
            category: item.category || '',
            series: item.series || '',
            summary: item.summary_ai || item.summary || '',
            snippet: item.snippet || '',
            score: Number(item.score || item.semantic_score || 0),
          })),
        }
      },
    }),
  )

  disposers.push(
    ctx.tools.register({
      name: 'kb_get',
      description:
        'Read one knowledge-base report by its path (get it from kb_search first): metadata, AI summary, key points, tags and a body preview (about the first 1200 characters).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          path: { type: 'string', description: 'Report path like "20261104/a-share-market-overview-20261104.html".' },
        },
        required: ['path'],
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            title: { type: 'string' },
            path: { type: 'string' },
            date: { type: 'string' },
            category: { type: 'string' },
            summary: { type: 'string' },
            key_points: { type: 'array', items: { type: 'string' } },
            tags: { type: 'array', items: { type: 'string' } },
            preview: { type: 'string' },
          },
          required: ['title', 'path', 'preview'],
        },
        render: (_args, value) =>
          textResult(
            [
              `《${value.title}》`,
              [value.date, value.category].filter(Boolean).join(' · '),
              value.summary ? `摘要：${value.summary}` : '',
              value.key_points && value.key_points.length ? '要点：\n- ' + value.key_points.join('\n- ') : '',
              value.tags && value.tags.length ? `标签：${value.tags.join('、')}` : '',
              '',
              value.preview,
              '',
              `路径：${value.path}`,
            ]
              .filter(Boolean)
              .join('\n'),
          ),
      },
      timeoutMs: 30_000,
      async execute(args) {
        const path = String(args.path || '').replace(/^\/+/, '')
        const data = await kbJson(`/api/kb/report/${encodeURI(path)}`)
        const item = data.item || {}
        return {
          title: item.title || '',
          path: item.path || path,
          date: item.date || '',
          category: item.category || '',
          summary: item.summary_ai || item.summary || '',
          key_points: (item.key_points || []).map(String).slice(0, 5),
          tags: (item.tags || []).map(String).slice(0, 8),
          preview: (item.preview || item.summary || '').slice(0, 2000),
        }
      },
    }),
  )

  disposers.push(
    ctx.tools.register({
      name: 'kb_ask',
      description:
        'Ask a question over the personal knowledge base: hybrid retrieval picks relevant reports, then the model answers with citations. ' +
        'Slower than kb_search (about 20-40s) but returns a grounded answer with [n] sources.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          question: { type: 'string', description: 'Natural-language question about the report library.' },
        },
        required: ['question'],
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            answer: { type: 'string' },
            mode: { type: 'string' },
            citations: { type: 'array', items: { type: 'object', additionalProperties: false, properties: { n: { type: 'integer' }, title: { type: 'string' }, path: { type: 'string' }, date: { type: 'string' } } } },
          },
          required: ['answer', 'mode', 'citations'],
        },
        render: (_args, value) =>
          textResult(
            [
              value.answer,
              '',
              value.citations && value.citations.length
                ? '来源：\n' + value.citations.map((cite) => `[${cite.n}] ${cite.title}${cite.date ? ` (${cite.date})` : ''} — ${cite.path}`).join('\n')
                : '',
            ]
              .filter(Boolean)
              .join('\n'),
          ),
      },
      timeoutMs: 180_000,
      async execute(args) {
        const data = await kbJson('/api/kb/ask', { method: 'POST', body: { question: String(args.question || '') }, timeoutMs: 170_000 })
        if (!data.ok) throw new Error(data.error || '知识库问答失败')
        return {
          answer: data.answer || '',
          mode: data.mode || 'hybrid',
          citations: (data.citations || []).map((cite) => ({
            n: Number(cite.n || 0),
            title: cite.title || '',
            path: cite.path || '',
            date: cite.date || '',
          })),
        }
      },
    }),
  )

  return () => disposers.forEach((dispose) => typeof dispose === 'function' && dispose())
}
