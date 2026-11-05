#!/usr/bin/env node
/**
 * dsh-kb host 半（`lib/index.js`）的隔离验收 —— 不需要把插件装进 DSH。
 *
 * 做法：mock 一个最小的 Cordis ctx（`get('connection')` / `effect` / `webServer.register`），
 * 调 `apply(ctx)` 拿到插件注册的 `/kb` 前缀 handler，再用 node:http 把它挂到临时端口上，
 * 从而在真实 HTTP 语义下验证：
 *   - /kb → 302 /kb/（保留 query）
 *   - /kb/、/kb/api/kb、/kb/static/* 反代到上游 200
 *   - 鉴权闸门：requestRejection 返回 401 / 403 时原样挡掉，不触达上游
 *   - 上游未启动时回 502（中文提示），进程不崩
 *
 * 上游用系统 python3.11 在 127.0.0.1:8916 拉起裁剪版 FastAPI（与 verify-app.mjs 相同），
 * 退出前 kill 掉子进程。
 *
 * 用法：node verification/verify-proxy.mjs [--port 8916]
 */

import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SERVER_DIR = resolve(HERE, '..', 'server')
const PYTHON = process.env.DSH_KB_PYTHON || '/usr/bin/python3.11'
const UPSTREAM_PORT = Number((() => {
  const index = process.argv.indexOf('--port')
  return index !== -1 ? process.argv[index + 1] : 8916
})())
process.env.DSH_KB_UPSTREAM_PORT = String(UPSTREAM_PORT)

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`)
}

async function waitForServer(url, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      if ((await fetch(url, { signal: AbortSignal.timeout(500) })).status < 500) return true
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  return false
}

async function stop(child) {
  if (!child || child.exitCode !== null) return
  child.kill('SIGTERM')
  const exited = await Promise.race([
    new Promise((resolve) => child.once('exit', () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), 4000)),
  ])
  if (!exited && child.exitCode === null) child.kill('SIGKILL')
}

async function main() {
  if (await waitForServer(`http://127.0.0.1:${UPSTREAM_PORT}/api/kb`, 500)) {
    console.error(`[verify-proxy] 127.0.0.1:${UPSTREAM_PORT} 已被占用，请先停掉再跑。`)
    process.exit(2)
  }

  const child = spawn(
    PYTHON,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(UPSTREAM_PORT), '--log-level', 'warning'],
    { cwd: SERVER_DIR, env: { ...process.env, PYTHONUNBUFFERED: '1' }, stdio: ['ignore', 'pipe', 'pipe'] },
  )
  let upstreamLog = ''
  child.stdout.on('data', (chunk) => { upstreamLog += chunk.toString() })
  child.stderr.on('data', (chunk) => { upstreamLog += chunk.toString() })

  let handler = null
  const disposers = []
  const ctx = {
    get: (name) => (name === 'connection' ? { requestRejection: () => currentRejection } : undefined),
    effect: (fn) => {
      const dispose = typeof fn === 'function' ? fn() : undefined
      disposers.push(dispose)
    },
    webServer: {
      register: (options) => {
        handler = options.handler
        return () => {}
      },
    },
    tools: { register: () => () => {} },
  }
  let currentRejection = undefined

  let proxy = null
  try {
    if (!(await waitForServer(`http://127.0.0.1:${UPSTREAM_PORT}/api/kb`))) {
      console.error('[verify-proxy] 上游启动失败：\n' + upstreamLog)
      process.exit(2)
    }
    const { apply, name, inject } = await import('../lib/index.js')
    check('lib/index.js 导出 name/inject/apply', name === 'dsh-kb' && inject.includes('webServer') && typeof apply === 'function')
    apply(ctx)
    check('apply(ctx) 注册了 /kb 前缀 handler', typeof handler === 'function')

    proxy = createServer((req, res) => handler(req, res))
    await new Promise((resolve) => proxy.listen(0, '127.0.0.1', resolve))
    const port = proxy.address().port
    const base = `http://127.0.0.1:${port}`

    const page = await fetch(base + '/kb/')
    const pageText = await page.text()
    check('/kb/ 反代 200 且返回知识库页面', page.status === 200 && pageText.includes('知识库'), `status=${page.status}`)

    const api = await fetch(base + '/kb/api/kb?refresh=1')
    const apiJson = await api.json()
    check('/kb/api/kb 反代 200 且 items>0', api.status === 200 && Array.isArray(apiJson.items) && apiJson.items.length > 0,
      `status=${api.status} items=${apiJson.items?.length}`)

    const css = await fetch(base + '/kb/static/style.css')
    check('/kb/static/* 反代 200', css.status === 200 && (css.headers.get('content-type') || '').includes('css'),
      `status=${css.status} type=${css.headers.get('content-type')}`)

    const redirect = await fetch(base + '/kb?x=1', { redirect: 'manual' })
    check('/kb → 302 /kb/ 且保留 query', [301, 302, 307, 308].includes(redirect.status) &&
      redirect.headers.get('location') === '/kb/?x=1', `status=${redirect.status} location=${redirect.headers.get('location')}`)

    currentRejection = 401
    const denied = await fetch(base + '/kb/api/kb')
    const deniedText = await denied.text()
    check('未登录（401）请求被鉴权闸门挡掉', denied.status === 401 && deniedText.includes('authentication required'),
      `status=${denied.status}`)

    currentRejection = 403
    const forbidden = await fetch(base + '/kb/api/kb')
    check('不可信来源（403）请求被鉴权闸门挡掉', forbidden.status === 403, `status=${forbidden.status}`)

    currentRejection = undefined
    const aiNoSecret = await fetch(base + '/kb/_internal/ai/status')
    check('AI 桥无密钥时按 404 隐藏', aiNoSecret.status === 404, `status=${aiNoSecret.status}`)
    const aiWrongSecret = await fetch(base + '/kb/_internal/ai/status', { headers: { 'x-kb-ai-secret': 'wrong' } })
    check('AI 桥错误密钥同样 404', aiWrongSecret.status === 404, `status=${aiWrongSecret.status}`)

    await stop(child)
    const down = await fetch(base + '/kb/api/kb')
    const downText = await down.text()
    check('上游未启动时回 502 中文提示', down.status === 502 && downText.includes('知识库服务暂时不可用'),
      `status=${down.status}`)
  } catch (error) {
    check('代理验收流程未抛异常', false, error && error.stack ? error.stack.split('\n')[0] : String(error))
  } finally {
    await stop(child)
    for (const dispose of disposers) {
      try {
        if (typeof dispose === 'function') dispose()
      } catch {}
    }
    if (proxy) await new Promise((resolve) => proxy.close(resolve))
    }

  mkdirSync(HERE, { recursive: true })
  const passed = results.filter((item) => item.ok).length
  console.log(`\n${passed}/${results.length} passed`)
  for (const item of results.filter((entry) => !entry.ok)) console.log(`FAILED: ${item.name}${item.detail ? ` — ${item.detail}` : ''}`)
  process.exit(results.every((item) => item.ok) ? 0 : 1)
}

main().catch((error) => {
  console.error('[verify-proxy] 未捕获异常:', error)
  process.exit(1)
})
