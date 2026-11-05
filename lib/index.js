/**
 * dsh-kb —— host 半：把本机知识库后端（FastAPI，127.0.0.1:8816）挂到
 * DSH Web 的 `/kb` 前缀下，并复用 DSH 自己的登录闸门。
 *
 * 设计要点（与 dsh-ziwei 的 /ziwei 反代一致）：
 *  - 只在 127.0.0.1 上反代，不碰公网；后端进程由 Lead 的 systemd 单元拉起。
 *  - 每个请求先过 `ctx.connection.requestRejection()`：403（Host/Origin 不在
 *    信任围栏内）/ 401（未通过浏览器登录）/ undefined（放行）。内嵌面板因此
 *    天然继承 DSH 的 token/cookie 会话，不需要另做一套认证。
 *  - 反代不剥前缀：后端应用自身同时注册了 `/` 与 `/kb/` 两套路由，`/kb/*`
 *    原样转发即可；直连 8916 时用 `/kb/` 也能逐字节复现 iframe 里的行为。
 *  - `/kb`（无尾斜杠）302 到 `/kb/`，保留 query，避免相对路径解析错基准。
 */
import { request as httpRequest } from 'node:http'
import { ensureSecret, handleInternalAi, isInternalAiPath } from './ai-bridge.js'
import { registerKbTools } from './tools.js'

const UPSTREAM_HOST = '127.0.0.1'
/** 正式端口 8816；测试时可用 DSH_KB_UPSTREAM_PORT 指向临时后端。 */
const UPSTREAM_PORT = Number(process.env.DSH_KB_UPSTREAM_PORT || 8816)
const ROUTE_PATH = '/kb'
/** 上游响应超时（毫秒）：本地 FastAPI 正常响应远小于这个值。 */
const UPSTREAM_TIMEOUT_MS = 10_000
/** RAG 问答与周报生成要调模型，耗时可达数十秒，单独放宽。 */
const LONG_RUNNING_TIMEOUT_MS = 180_000
const LONG_RUNNING_PATH = /^\/kb\/api\/kb\/(ask|digest)\b/

/** RFC 7230 hop-by-hop 头，不该被代理透传。 */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
])

/** 转发给上游的请求头：剥掉逐跳头，补上 X-Forwarded-*。 */
function upstreamHeaders(req) {
  const out = {}
  for (const [key, value] of Object.entries(req.headers)) {
    if (value === undefined) continue
    if (HOP_BY_HOP.has(key.toLowerCase())) continue
    out[key] = value
  }
  out.host = `${UPSTREAM_HOST}:${UPSTREAM_PORT}`
  out['x-forwarded-host'] = req.headers.host || `${UPSTREAM_HOST}:${UPSTREAM_PORT}`
  out['x-forwarded-proto'] = 'http'
  if (req.socket && req.socket.remoteAddress) {
    out['x-forwarded-for'] = req.socket.remoteAddress
  }
  return out
}

/** 上游响应头：同样剥掉逐跳头，交给 node 自己算 Transfer-Encoding。 */
function downstreamHeaders(upstreamRes) {
  const out = {}
  for (const [key, value] of Object.entries(upstreamRes.headers)) {
    if (value === undefined) continue
    if (HOP_BY_HOP.has(key.toLowerCase())) continue
    out[key] = value
  }
  return out
}

function sendPlain(res, status, text) {
  if (res.headersSent) {
    res.end()
    return
  }
  res.writeHead(status, {
    'content-type': 'text/plain; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(text)
}

/** 鉴权闸门：没登录 / 主机不可信就直接挡掉，绝不把本地应用暴露出去。 */
function rejectUnprivileged(ctx, req, res) {
  let rejection
  try {
    const connection = ctx.get('connection', false)
    rejection =
      connection && typeof connection.requestRejection === 'function'
        ? connection.requestRejection(req)
        : 401
  } catch {
    rejection = 401
  }
  if (!rejection) return false
  const text =
    rejection === 403
      ? 'dsh web: 该请求的主机/来源不在信任列表内。\n'
      : 'dsh web authentication required; reopen the URL printed by dsh web.\n'
  sendPlain(res, rejection, text)
  return true
}

/** 把 req.url 拆成 path 与 `?query`（query 含问号，没有则空串）。 */
function splitUrl(url) {
  const raw = String(url || '')
  const index = raw.indexOf('?')
  if (index === -1) return { pathname: raw, query: '' }
  return { pathname: raw.slice(0, index), query: raw.slice(index) }
}

/** `/kb` 前缀 → 上游 127.0.0.1:8816 的流式反代。 */
function createHandler(ctx) {
  return function kbProxy(req, res) {
    // AI 桥：只接受回环 + 共享密钥，不走浏览器登录闸门（后端进程没有浏览器会话）。
    if (isInternalAiPath(req.url)) {
      handleInternalAi(ctx, req, res).catch((error) => {
        console.warn('[dsh-kb] AI 桥异常:', error && error.message ? error.message : error)
        if (!res.headersSent) {
          res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' })
          res.end(JSON.stringify({ ok: false, error: 'AI 桥内部错误' }))
        }
      })
      return
    }
    if (rejectUnprivileged(ctx, req, res)) return

    const { pathname, query } = splitUrl(req.url)
    if (pathname === ROUTE_PATH) {
      // 兜住「无尾斜杠」：iframe / 手敲地址都落到带斜杠版本，相对路径才对。
      res.writeHead(302, { location: `${ROUTE_PATH}/${query}`, 'cache-control': 'no-store' })
      res.end()
      return
    }

    const upstream = httpRequest(
      {
        host: UPSTREAM_HOST,
        port: UPSTREAM_PORT,
        method: req.method,
        path: req.url,
        headers: upstreamHeaders(req),
      },
      (upstreamRes) => {
        res.writeHead(upstreamRes.statusCode || 502, downstreamHeaders(upstreamRes))
        upstreamRes.pipe(res)
        upstreamRes.on('error', () => res.destroy())
      },
    )

    // 上游卡住（不响应）时不能把 handler 永久挂起：超时直接 504。
    const timeoutMs = LONG_RUNNING_PATH.test(req.url || '') ? LONG_RUNNING_TIMEOUT_MS : UPSTREAM_TIMEOUT_MS
    upstream.setTimeout(timeoutMs, () => {
      upstream.destroy(Object.assign(new Error('上游响应超时'), { code: 'ETIMEDOUT' }))
    })

    upstream.on('error', (error) => {
      if (error && error.code === 'ETIMEDOUT') {
        console.warn(`[dsh-kb] 反代超时（${timeoutMs}ms）:`, req.method, req.url)
        sendPlain(res, 504, '知识库服务响应超时，请稍后重试。\n')
        return
      }
      const reason = error && error.code ? error.code : 'ECONNREFUSED'
      // 诊断信息只写宿主日志：回给浏览器的文案不带错误码、单元名、路径。
      console.warn(
        `[dsh-kb] 反代失败（${reason}）: ${req.method} ${req.url} → 127.0.0.1:${UPSTREAM_PORT}`,
      )
      sendPlain(res, 502, '知识库服务暂时不可用，请稍后重试。\n')
    })

    req.on('aborted', () => upstream.destroy())
    res.on('close', () => upstream.destroy())
    req.pipe(upstream)
  }
}

export const name = 'dsh-kb'
export const inject = ['webServer', 'tools']

export function apply(ctx) {
  // 生成/复用后端共享的 AI 桥密钥（server/.ai-secret，0600）。
  try {
    ensureSecret()
  } catch (error) {
    console.warn('[dsh-kb] 无法准备 AI 桥密钥:', error && error.message ? error.message : error)
  }
  const handler = createHandler(ctx)
  ctx.effect(() => {
    const dispose = ctx.webServer.register({
      kind: 'prefix',
      path: ROUTE_PATH,
      handler,
    })
    return () => {
      if (typeof dispose === 'function') dispose()
    }
  }, 'dsh-kb: /kb reverse proxy')

  // P4：把 kb_search / kb_get / kb_ask 注册成 DSH 工具（供 agent 调用）。
  ctx.effect(() => registerKbTools(ctx), 'dsh-kb: knowledge-base tools')
}
