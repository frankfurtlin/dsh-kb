/**
 * dsh-kb —— AI 桥（host 半）：把 DSH 自己的 LLM（dsh-agent 的默认模型）借给
 * 知识库后端用，后端不再直接持有任何模型 API key。
 *
 * 链路：
 *   知识库后端（FastAPI :8816） --POST /kb/_internal/ai--> 本桥 --ctx.llm.stream--> DSH 模型
 *
 * 安全：
 *  - 只接受来自回环地址（127.0.0.1 / ::1）的请求；
 *  - 必须带 `x-kb-ai-secret`，值来自 server/.ai-secret（0600，首次加载时生成）；
 *  - 该路径在 DSH 登录闸门之前单独处理，不会把 LLM 暴露给浏览器会话。
 *
 * 之所以走 host 插件而不是让后端直接连 DSH：DSH 的鉴权 cookie 属于浏览器会话，
 * 后端进程没有；而 host 插件里可以直接拿到 `llm` / `agentDefaultModel` 服务。
 */
import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** 插件的 server/ 目录：密钥文件与后端共用。 */
export const SERVER_DIR = fileURLToPath(new URL('../server/', import.meta.url))
export const SECRET_PATH = path.join(SERVER_DIR, '.ai-secret')

export const INTERNAL_AI_PATH = '/kb/_internal/ai'
/** 流式版本：SSE 逐 token 回吐，前端首字延迟从十几秒降到 1–2 秒。 */
export const INTERNAL_AI_STREAM_PATH = '/kb/_internal/ai/stream'
/** opencode-go 路由此类 provider 需要会话标识（x-opencode-session）才能路由；用一个稳定的伪会话。 */
const BRIDGE_SESSION_ID = randomUUID()
export const INTERNAL_AI_STATUS_PATH = '/kb/_internal/ai/status'

/** 单次模型调用的上限：RAG 问答/周报可能慢，但必须有个边界。 */
const MODEL_TIMEOUT_MS = 180_000
/** 请求体上限：正文 + 提示词，正常远小于这个值。 */
const BODY_LIMIT = 512 * 1024

/** 保证密钥文件存在；返回当前密钥（每次读文件，宿主重启换密钥时后端自动跟上）。 */
export function ensureSecret() {
  try {
    const value = readFileSync(SECRET_PATH, 'utf8').trim()
    if (value.length >= 32) return value
  } catch {
    // 文件不存在或不可读：下面生成新的。
  }
  mkdirSync(SERVER_DIR, { recursive: true })
  const secret = randomBytes(32).toString('hex')
  writeFileSync(SECRET_PATH, secret + '\n', { mode: 0o600 })
  return secret
}

function readSecret() {
  try {
    return readFileSync(SECRET_PATH, 'utf8').trim()
  } catch {
    return ''
  }
}

function isLoopback(req) {
  const address = req.socket && req.socket.remoteAddress
  return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1'
}

function sameSecret(provided, expected) {
  if (!provided || !expected) return false
  const a = Buffer.from(String(provided))
  const b = Buffer.from(expected)
  return a.length === b.length && timingSafeEqual(a, b)
}

function sendJson(res, status, payload) {
  if (res.headersSent) {
    res.end()
    return
  }
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(JSON.stringify(payload))
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > BODY_LIMIT) {
        reject(new Error('请求体过大'))
        req.pause()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

/** 当前默认模型（provider/model）：优先 DSH 的默认选择，其次模型服务列表里的第一条。 */
function resolveTarget(ctx, body) {
  if (body && typeof body.provider === 'string' && typeof body.model === 'string' && body.provider && body.model) {
    return { provider: body.provider, model: body.model, source: 'request' }
  }
  const selection = ctx.get('agentDefaultModel', false)
  if (selection && typeof selection.currentSelection === 'function') {
    const current = selection.currentSelection()
    if (current && current.provider && current.model) {
      return { provider: current.provider, model: current.model, source: 'agentDefaultModel' }
    }
  }
  const llm = ctx.get('llm', false)
  if (llm && typeof llm.listProviders === 'function') {
    const providers = llm.listProviders() || []
    for (const provider of providers) {
      if (provider && provider.id) {
        return { provider: provider.id, model: '', source: 'llm-providers' }
      }
    }
  }
  return undefined
}

/** 准备一次模型调用的 stream：返回 { iterable, target } 或抛错。 */
async function openStream(ctx, target, { system, user, maxTokens, temperature, signal, reasoningEffort }) {
  const llm = ctx.get('llm', false)
  if (!llm || typeof llm.stream !== 'function') {
    throw new Error('DSH 的 llm 服务不可用')
  }
  if (!target || !target.model) {
    throw new Error('DSH 没有可用的默认模型：请先在 DSH 里选择模型')
  }
  // 与 DSH 内部调用（compaction/session-title）一致：先 prepareCall 解析出
  // 可路由的 call 配置，再带完整消息对象调用 call.stream。
  const message = Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text: user }],
    source: { kind: 'dsh-kb' },
  })
  const limit = Math.max(256, Math.min(Number(maxTokens) || 1200, 8000))
  const prepareConfig = { provider: target.provider, model: target.model, maxTokens: limit }
  if (typeof temperature === 'number' && Number.isFinite(temperature)) prepareConfig.temperature = temperature
  // 知识库的问答只需要摘要级推理：低推理强度能明显降低首 token 时间。
  if (reasoningEffort === 'low' || reasoningEffort === 'medium' || reasoningEffort === 'high') {
    prepareConfig.reasoningEffort = reasoningEffort
  }
  const call =
    typeof llm.prepareCall === 'function' ? await llm.prepareCall(prepareConfig, signal) : undefined
  // 与 DSH 内部辅助调用一致：call.config 是唯一可信的请求配置，额外字段只加
  // messages/system/sessionId/signal（适配器禁止再改动已准备的配置）。
  const options = {
    ...(call && call.config ? call.config : prepareConfig),
    messages: [message],
    sessionId: BRIDGE_SESSION_ID,
    signal,
  }
  if (typeof system === 'string' && system.length > 0) options.system = system
  return call && typeof call.stream === 'function' ? call.stream(options) : llm.stream(options)
}

/** 用 DSH 的 LLM 服务跑一次「system + user → 文本」调用，只收集正文 token。 */
async function generateText(ctx, target, options) {
  const stream = await openStream(ctx, target, options)
  let text = ''
  const kinds = new Set()
  let tail = ''
  for await (const chunk of stream) {
    if (!chunk || typeof chunk.type !== 'string') continue
    kinds.add(chunk.type)
    if (chunk.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text
    if (chunk.type === 'finish' || chunk.type === 'block-end') tail = JSON.stringify(chunk).slice(0, 400)
  }
  if (!text.trim() && kinds.size > 0) {
    throw new Error(`模型返回空内容（chunks: ${[...kinds].join(',')}; 结尾: ${tail}）`)
  }
  return text
}

/** 流式版本：逐段产出 {kind: 'text' | 'reasoning', text}。 */
async function* streamText(ctx, target, options) {
  const stream = await openStream(ctx, target, options)
  for await (const chunk of stream) {
    if (!chunk) continue
    if (chunk.type === 'text-delta' && typeof chunk.text === 'string' && chunk.text) {
      yield { kind: 'text', text: chunk.text }
    } else if (chunk.type === 'reasoning-delta' && typeof chunk.text === 'string' && chunk.text) {
      yield { kind: 'reasoning', text: chunk.text }
    }
  }
}

/** 解析 provider/model（含 listModels 兜底），返回 { target } 或 { error }。 */
async function resolveRoute(ctx, body) {
  const target = resolveTarget(ctx, body)
  if (!target) return { error: 'DSH 没有可用的默认模型：请先在 DSH 里选择模型' }
  if (!target.model) {
    const llm = ctx.get('llm', false)
    let model = ''
    try {
      const models = llm && typeof llm.listModels === 'function' ? await llm.listModels(target.provider) : []
      if (Array.isArray(models) && models.length > 0) model = models[0].id || models[0].model || ''
    } catch {
      // 拿不到模型列表时留空，下面统一报错。
    }
    if (!model) return { error: 'DSH 的默认模型未配置' }
    target.model = model
  }
  return { target }
}

/** 处理 `/kb/_internal/ai` 与 `/kb/_internal/ai/status`；返回 true 表示已接管响应。 */
export async function handleInternalAi(ctx, req, res) {
  const raw = String(req.url || '')
  const pathname = raw.split('?')[0]
  if (
    pathname !== INTERNAL_AI_PATH &&
    pathname !== INTERNAL_AI_STATUS_PATH &&
    pathname !== INTERNAL_AI_STREAM_PATH
  )
    return false

  if (!isLoopback(req)) {
    sendJson(res, 404, { ok: false, error: 'not found' })
    return true
  }
  if (!sameSecret(req.headers['x-kb-ai-secret'], readSecret())) {
    sendJson(res, 404, { ok: false, error: 'not found' })
    return true
  }

  if (pathname === INTERNAL_AI_STATUS_PATH) {
    const target = resolveTarget(ctx, undefined)
    sendJson(res, 200, {
      ok: true,
      mode: 'dsh-agent',
      provider: target ? target.provider : '',
      model: target ? target.model : '',
    })
    return true
  }

  if (req.method !== 'POST') {
    sendJson(res, 405, { ok: false, error: 'method not allowed' })
    return true
  }

  let body
  try {
    body = JSON.parse(await readBody(req))
  } catch (error) {
    sendJson(res, 400, { ok: false, error: `请求体不是合法 JSON：${error.message}` })
    return true
  }
  const user = typeof body.user === 'string' ? body.user : ''
  if (!user.trim()) {
    sendJson(res, 400, { ok: false, error: '缺少 user 提示词' })
    return true
  }

  const route = await resolveRoute(ctx, body)
  if (route.error) {
    sendJson(res, 200, { ok: false, error: route.error })
    return true
  }
  const target = route.target
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('模型调用超时')), MODEL_TIMEOUT_MS)
  const modelOptions = {
    system: body.system,
    user,
    maxTokens: body.max_tokens,
    temperature: body.temperature,
    reasoningEffort: body.reasoning_effort,
    signal: controller.signal,
  }

  if (pathname === INTERNAL_AI_STREAM_PATH) {
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-store',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    })
    try {
      let reasoningChars = 0
      let sentReasoning = 0
      for await (const part of streamText(ctx, target, modelOptions)) {
        if (!part) continue
        if (part.kind === 'reasoning') {
          reasoningChars += part.text.length
          if (reasoningChars - sentReasoning >= 120) {
            sentReasoning = reasoningChars
            res.write(`event: thinking\ndata: ${JSON.stringify({ chars: reasoningChars })}\n\n`)
          }
          continue
        }
        res.write(`event: delta\ndata: ${JSON.stringify({ text: part.text })}\n\n`)
      }
      res.write(
        `event: done\ndata: ${JSON.stringify({
          ok: true,
          provider: target.provider,
          model: target.model,
        })}\n\n`,
      )
    } catch (error) {
      const message = error && error.message ? error.message : String(error)
      console.warn(`[dsh-kb] AI 桥流式调用失败: ${message}`)
      res.write(`event: error\ndata: ${JSON.stringify({ ok: false, error: message })}\n\n`)
    } finally {
      clearTimeout(timer)
      res.end()
    }
    return true
  }

  try {
    const text = await generateText(ctx, target, modelOptions)
    if (!text.trim()) {
      sendJson(res, 200, { ok: false, error: '模型返回空内容' })
      return true
    }
    sendJson(res, 200, {
      ok: true,
      text,
      provider: target.provider,
      model: target.model,
      mode: 'dsh-agent',
    })
  } catch (error) {
    const message = error && error.message ? error.message : String(error)
    console.warn(`[dsh-kb] AI 桥调用失败: ${message}`)
    sendJson(res, 200, { ok: false, error: message })
  } finally {
    clearTimeout(timer)
  }
  return true
}

export function isInternalAiPath(url) {
  const pathname = String(url || '').split('?')[0]
  return (
    pathname === INTERNAL_AI_PATH ||
    pathname === INTERNAL_AI_STATUS_PATH ||
    pathname === INTERNAL_AI_STREAM_PATH
  )
}
