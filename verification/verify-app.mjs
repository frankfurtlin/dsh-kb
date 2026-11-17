#!/usr/bin/env node
/**
 * dsh-kb 端到端验收（直连后端，不依赖 DSH 反代）。
 *
 * 脚本用系统 python3.11 在 127.0.0.1:8916 拉起 FastAPI（KB_DATA_DIR 指向临时目录，
 * 不碰生产索引库），再用 Playwright 走一遍真实用户流程：层次导航（分类/系列/日期）→
 * 全文检索与高亮 → 排序/分页 → 详情抽屉（已读/收藏）→ 打开原文 → 移动端。
 *
 * 关键约束：
 *  - 只监听 127.0.0.1:8916；脚本退出前一定杀掉自己拉起的 uvicorn。
 *  - 页面以 `http://127.0.0.1:8916/kb/` 打开，等价于 DSH iframe 里的相对路径环境。
 *  - 断言页面与报告正文中不残留 `/mine/...` 绝对路径。
 *
 * 用法：node verification/verify-app.mjs [--url http://127.0.0.1:8916/kb/] [--out shots]
 * 退出码：0 = 全部通过，1 = 有 FAIL，2 = 环境/启动失败。
 */

import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SERVER_DIR = resolve(HERE, '..', 'server')
const PYTHON = process.env.DSH_KB_PYTHON || '/usr/bin/python3.11'
const require = createRequire('/root/workspace/hermes-site/tools/node_modules/')
const { chromium } = require('playwright')

// Playwright 与浏览器 revision 可能失配（hermes-site/tools 会升级）：优先用环境变量，
// 否则在默认缺失时回退到本机已有的 headless shell。
const FALLBACK_CHROME =
  '/root/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'

async function launchBrowser() {
  const options = { headless: true, args: ['--no-sandbox'] }
  if (process.env.KB_CHROMIUM) return chromium.launch({ ...options, executablePath: process.env.KB_CHROMIUM })
  try {
    return await chromium.launch(options)
  } catch (error) {
    if (!String(error).includes('Executable doesn\'t exist')) throw error
    console.warn('[playwright] 默认浏览器缺失，回退到 chromium 1243：' + FALLBACK_CHROME)
    return chromium.launch({ ...options, executablePath: FALLBACK_CHROME })
  }
}


function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`)
  if (index === -1) return fallback
  const value = process.argv[index + 1]
  return value && !value.startsWith('--') ? value : fallback
}

const PAGE_URL = arg('url', 'http://127.0.0.1:8916/kb/')
const OUT = resolve(arg('out', join(HERE, 'shots')))
const ORIGIN = new URL(PAGE_URL).origin
const PORT = Number(new URL(PAGE_URL).port || 80)
const results = []
let DATA_DIR = ''

function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`)
}

async function shot(page, name, fullPage = false) {
  await page.screenshot({ path: join(OUT, name), fullPage })
}

async function waitForServer(url, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1000) })
      if (response.status < 500) return true
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  return false
}

async function stopServer(child) {
  if (!child || child.exitCode !== null) return
  child.kill('SIGTERM')
  await Promise.race([
    new Promise((resolve) => child.once('exit', resolve)),
    new Promise((resolve) => setTimeout(resolve, 4000)),
  ])
  if (child.exitCode === null) child.kill('SIGKILL')
}

async function api(path, options = undefined) {
  const response = await fetch(ORIGIN + path, options)
  return { status: response.status, json: response.headers.get('content-type')?.includes('json') ? await response.json() : null }
}

async function postState(body) {
  const response = await fetch(ORIGIN + '/api/kb/state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return { status: response.status, json: await response.json().catch(() => null) }
}


async function seedAi(reportPath, payload) {
  const env = { ...process.env, KB_DATA_DIR: DATA_DIR }
  const code = [
    'from app.services import kb_store',
    `kb_store.set_ai(${JSON.stringify(reportPath)}, ${JSON.stringify(payload)})`,
    'print("seeded")',
  ].join('\n')
  return new Promise((resolvePromise) => {
    const child = spawn(PYTHON, ['-c', code], { cwd: SERVER_DIR, env })
    let output = ''
    child.stdout.on('data', (chunk) => { output += chunk.toString() })
    child.stderr.on('data', (chunk) => { output += chunk.toString() })
    seedLog = output
    child.on('exit', (codeValue) => resolvePromise(codeValue === 0 && output.includes('seeded')))
    child.on('error', () => resolvePromise(false))
  })
}

async function seedClassify(reportPath, payload) {
  const env = { ...process.env, KB_DATA_DIR: DATA_DIR }
  const code = [
    'from app.services import kb_store',
    `kb_store.set_classify(${JSON.stringify(reportPath)}, ${JSON.stringify(payload)}, model="verify-app")`,
    'print("seeded")',
  ].join('\n')
  return new Promise((resolvePromise) => {
    const child = spawn(PYTHON, ['-c', code], { cwd: SERVER_DIR, env })
    let output = ''
    child.stdout.on('data', (chunk) => { output += chunk.toString() })
    child.stderr.on('data', (chunk) => { output += chunk.toString() })
    seedLog = output
    child.on('exit', (codeValue) => resolvePromise(codeValue === 0 && output.includes('seeded')))
    child.on('error', () => resolvePromise(false))
  })
}

let seedLog = ''

async function main() {
  mkdirSync(OUT, { recursive: true })
  DATA_DIR = mkdtempSync(join(tmpdir(), 'dsh-kb-verify-'))
  const child = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'warning'], {
    cwd: SERVER_DIR,
    env: { ...process.env, KB_DATA_DIR: DATA_DIR, KB_EMBED_ENABLED: '0', KB_AI_SECRET_FILE: join(DATA_DIR, 'no-ai-bridge-secret') },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let serverLog = ''
  child.stdout.on('data', (chunk) => { serverLog += chunk.toString() })
  child.stderr.on('data', (chunk) => { serverLog += chunk.toString() })
  let browser = null
  try {
    const up = await waitForServer(ORIGIN + '/api/kb/tree')
    check(`后端启动 (${ORIGIN})`, up, up ? '' : serverLog.slice(-500))
    if (!up) throw new Error('后端未启动')

    const tree = (await api('/api/kb/tree')).json
    check('GET /api/kb/tree 200 且有分类/系列/日期', tree && tree.total > 0 && tree.categories.length > 0 && tree.series.length > 0,
      `total=${tree?.total} cats=${tree?.categories?.length} series=${tree?.series?.length}`)
    check('GET /api/kb 200 且 items>0', (await api('/api/kb')).json?.items?.length > 0)
    check('GET /kb/api/kb 200（反代前缀同样可用）', (await api('/kb/api/kb')).status === 200)
    check('GET /kb 无尾斜杠 → 重定向 /kb/', await (async () => {
      const response = await fetch(ORIGIN + '/kb', { redirect: 'manual' })
      return [302, 307, 308].includes(response.status) && (response.headers.get('location') || '').endsWith('/kb/')
    })())
    const reportPath = (await api('/api/kb?limit=1')).json.items[0].path
    check('GET /api/kb/report/<path> 200 且含 preview',
      (await api('/api/kb/report/' + reportPath.split('/').map(encodeURIComponent).join('/'))).json?.item?.preview?.length > 40)

    // ---------- P0/P1：健康检查 / 统一错误体 / 静态缓存与压缩 ----------
    const health = await api('/healthz')
    check('P0 GET /healthz 返回版本、统计与能力',
      health.status === 200 && health.json?.ok === true && /^\d+\.\d+\.\d+/.test(health.json?.version || '') &&
        health.json?.reports?.total > 0 && typeof health.json?.ai?.configured === 'boolean',
      JSON.stringify(health.json).slice(0, 90))
    const missing = await api('/reports/does-not-exist.html')
    check('P1 未命中报告返回统一错误体',
      missing.status === 404 && missing.json?.ok === false && typeof missing.json?.error === 'string',
      JSON.stringify(missing.json))
    const versioned = await fetch(`${ORIGIN}/static/kb.js?v=test1234`)
    check('P0 带指纹的静态资源走 immutable 长缓存',
      (versioned.headers.get('cache-control') || '').includes('immutable'),
      versioned.headers.get('cache-control') || '(none)')
    const plain = await fetch(`${ORIGIN}/static/kb.js`)
    check('P0 无指纹的静态资源不给长缓存',
      (plain.headers.get('cache-control') || '').includes('max-age=300'),
      plain.headers.get('cache-control') || '(none)')
    const gzipped = await fetch(`${ORIGIN}/api/kb?limit=20`, { headers: { 'accept-encoding': 'gzip' } })
    check('P1 JSON 响应经 gzip 压缩',
      (gzipped.headers.get('content-encoding') || '') === 'gzip',
      gzipped.headers.get('content-encoding') || '(none)')

    // ---------- P1：AI sidecar（直接种进临时索引库，验收不依赖模型/网络） ----------
    const aiSeeded = await seedAi(reportPath, {
      summary_ai: '这是一条 AI 摘要：报告复盘了今日 A 股主要指数的涨跌与资金流向，并给出次日观察点。',
      key_points: ['三大指数集体上涨', '成交额放大到 2 万亿', '资金向头部集中'],
      entities: [{ type: '指数', name: '上证指数' }, { type: '板块', name: '半导体' }],
      tags: ['A股', '复盘', '资金流向', '半导体', '风险提示'],
    })
    check('P1 索引库可写入 AI sidecar', aiSeeded, aiSeeded ? '' : seedLog.slice(-300))
    const aiStats = (await api('/api/kb/ai/stats')).json
    check('P1 GET /api/kb/ai/stats 统计正确', aiStats?.total > 0 && aiStats?.enriched === 1,
      `enriched=${aiStats?.enriched} total=${aiStats?.total} configured=${aiStats?.configured}`)
    const aiReport = (await api('/api/kb/report/' + reportPath.split('/').map(encodeURIComponent).join('/'))).json?.item
    check('P1 报告详情返回摘要/要点/标签/实体',
      (aiReport?.summary_ai || '').startsWith('这是一条 AI 摘要') &&
      (aiReport?.key_points || []).length === 3 && (aiReport?.tags || []).length === 5 &&
      (aiReport?.entities || []).length === 2, `tags=${(aiReport?.tags || []).length}`)
    check('P1 列表接口返回 AI 字段', ((await api('/api/kb?limit=1')).json?.items?.[0]?.tags || []).length === 5)

    // ---------- v0.3：标签关系 / 时间戳 ----------
    const tagList = (await api('/api/kb/tags')).json?.tags || []
    check('v0.3 GET /api/kb/tags 返回标签关系',
      tagList.some((row) => row.name === 'A股' && row.reports >= 1), `tags=${tagList.length}`)
    const tagFiltered = (await api('/api/kb?tag=' + encodeURIComponent('A股'))).json
    check('v0.3 按标签筛选列表',
      tagFiltered?.total >= 1 && (tagFiltered.items || []).every((item) => (item.tags || []).includes('A股')),
      `total=${tagFiltered?.total}`)
    const treeTag = (await api('/api/kb/tree?tag=' + encodeURIComponent('A股'))).json
    check('v0.3 树的 total 随标签收敛', treeTag?.total === tagFiltered?.total,
      `tree=${treeTag?.total} list=${tagFiltered?.total}`)
    const metaItem = (await api('/api/kb/report/' + reportPath.split('/').map(encodeURIComponent).join('/'))).json?.item
    check('v0.3 报告带入库/索引时间与模型字段',
      !!metaItem?.created_at && !!metaItem?.updated_at && 'ai_model' in metaItem)

    // ---------- R1：AI 分类（直接种进索引库，验收不依赖模型） ----------
    const classifyStats0 = (await api('/api/kb/classify/stats')).json
    check('R1 GET /api/kb/classify/stats 返回进度',
      typeof classifyStats0?.total === 'number' && typeof classifyStats0?.pending === 'number' && classifyStats0?.version >= 1,
      `pending=${classifyStats0?.pending} version=${classifyStats0?.version}`)
    const classifySeeded = await seedClassify(reportPath, { category: '市场全景', series: 'A股市场全景（验收）', reason: '验收种入' })
    check('R1 索引库可写入 AI 分类', classifySeeded, classifySeeded ? '' : seedLog.slice(-300))
    const classifyStats1 = (await api('/api/kb/classify/stats')).json
    check('R1 分类进度随写入推进', classifyStats1?.classified === 1 && classifyStats1?.pending === classifyStats0?.pending - 1,
      `classified=${classifyStats1?.classified} pending=${classifyStats1?.pending}`)
    const classifiedItem = (await api('/api/kb/report/' + reportPath.split('/').map(encodeURIComponent).join('/'))).json?.item
    check('R1 报告返回分类来源字段',
      classifiedItem?.category_ai === '市场全景' && classifiedItem?.category_rule === '市场全景' &&
      classifiedItem?.series_ai === 'A股市场全景（验收）' && classifiedItem?.classify_version >= 1,
      `ai=${classifiedItem?.category_ai} rule=${classifiedItem?.category_rule}`)
    const classifyEvents = (await api('/api/kb/events?path=' + encodeURIComponent(reportPath))).json?.events || []
    check('R1 分类写入留下 classify 事件',
      classifyEvents.some((event) => event.kind === 'classify' && event.payload?.source === 'ai'),
      classifyEvents.slice(0, 2).map((event) => event.kind).join(','))

    // ---------- P2/P3/P5：向量/问答/周报接口（临时后端无模型，验证降级路径） ----------
    const embedStats = await api('/api/kb/embed/stats')
    check('P2 GET /api/kb/embed/stats 返回模型与统计',
      embedStats.status === 200 && typeof embedStats.json?.available === 'boolean' && embedStats.json?.stats?.total > 0,
      `available=${embedStats.json?.available} total=${embedStats.json?.stats?.total}`)
    const hybrid = await api('/api/kb/search?q=' + encodeURIComponent('铝业') + '&mode=hybrid&limit=5')
    check('P2 混合检索可用（模型缺席时自动降级）',
      hybrid.status === 200 && hybrid.json?.total > 0 && ['hybrid', 'lexical'].includes(hybrid.json?.mode),
      `mode=${hybrid.json?.mode} total=${hybrid.json?.total}`)
    const ask = await api('/api/kb/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: '测试' }) })
    check('P3 问答在未配置模型时返回结构化错误',
      ask.status === 503 || ask.json?.ok === false, `status=${ask.status}`)
    const digest = await api('/api/kb/digest?days=90')
    check('P5 GET /api/kb/digest 返回窗口/主题/统计',
      digest.status === 200 && digest.json?.ok === true && digest.json?.total > 0 &&
      Array.isArray(digest.json?.themes) && Array.isArray(digest.json?.categories),
      `total=${digest.json?.total} themes=${digest.json?.themes?.length}`)

    // ---------- P6：公开分享（匿名只读外链） ----------
    const publicList = await api('/api/kb/public')
    check('P6 GET /api/kb/public 返回基址与集合',
      publicList.status === 200 && typeof publicList.json?.base === 'string' && Array.isArray(publicList.json?.paths),
      publicList.json?.base)
    const pubPair = (await api('/api/kb?limit=2')).json.items
    const pubPath = pubPair[0].path
    const privPath = pubPair[1].path
    const setPub = await api('/api/kb/public', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: pubPath, public: true }) })
    check('P6 设置公开成功并返回外链',
      setPub.status === 200 && setPub.json?.ok === true && setPub.json?.public === true && String(setPub.json?.url).includes('/kb-public/reports/'),
      setPub.json?.url)
    const pubResp = await fetch(ORIGIN + '/kb-public/reports/' + pubPath.split('/').map(encodeURIComponent).join('/'))
    const pubText = await pubResp.text()
    check('P6 公开报告可匿名访问且内链改写',
      pubResp.status === 200 && pubText.includes('/kb-public/reports/'), `${pubResp.status}`)
    const pubIndexResp = await fetch(ORIGIN + '/kb-public/')
    const pubIndexText = await pubIndexResp.text()
    check('P6 公开目录页列出该报告',
      pubIndexResp.status === 200 && pubIndexText.includes(pubPair[0].title.slice(0, 12)), `${pubIndexResp.status}`)
    const privResp = await fetch(ORIGIN + '/kb-public/reports/' + privPath.split('/').map(encodeURIComponent).join('/'))
    check('P6 未公开报告匿名访问 404', privResp.status === 404, `${privResp.status}`)
    const sharedAsset = await fetch(ORIGIN + '/kb-public/reports/assets/shell.css')
    check('P6 共享资源可匿名访问', sharedAsset.status === 200, `${sharedAsset.status}`)
    const apiLeak = await fetch(ORIGIN + '/kb-public/api/kb/tree')
    check('P6 公开前缀不暴露 /api/*', apiLeak.status === 404, `${apiLeak.status}`)
    const traversal = await fetch(ORIGIN + '/kb-public/reports/%2e%2e%2f%2e%2e%2fetc%2fpasswd')
    check('P6 公开路径拒绝目录穿越', traversal.status === 404, `${traversal.status}`)
    const unsetPub = await api('/api/kb/public', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: pubPath, public: false }) })
    check('P6 取消公开成功', unsetPub.status === 200 && unsetPub.json?.public === false)

    browser = await launchBrowser()
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const page = await context.newPage()
    const consoleErrors = []
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
    page.on('pageerror', (error) => consoleErrors.push('pageerror: ' + error.message))
    page.on('dialog', (dialog) => dialog.accept('验收筛选'))
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' })
    await page.waitForSelector('.kb-item', { timeout: 15000 })

    check('页面标题包含「知识库」', (await page.title()).includes('知识库'), await page.title())
    const pageHtml = await page.content()
    check('页面 HTML 无 /mine/ 绝对路径残留', !pageHtml.includes('/mine/'))
    check('P0 模板给静态资源加 ?v= 指纹', /static\/kb\.js\?v=[0-9a-f]{6,}/.test(pageHtml))
    check('P0 侧栏显示插件版本号', /^v\d+\.\d+\.\d+$/.test((await page.locator('#kb-side-version').innerText()).trim()),
      (await page.locator('#kb-side-version').innerText()).trim())

    // ---------- 层次导航 ----------
    const navCounts = await page.evaluate(() => ({
      all: document.getElementById('count-all')?.textContent?.trim(),
      unread: document.getElementById('count-unread')?.textContent?.trim(),
      starred: document.getElementById('count-starred')?.textContent?.trim(),
    }))
    check('左导航视图计数与 tree 一致',
      navCounts.all === String(tree.total) && navCounts.unread === String(tree.unread) && navCounts.starred === String(tree.starred),
      JSON.stringify(navCounts))
    const catButtons = await page.locator('#kb-cats .kb-cat').count()
    check('分类列表数量 = tree 分类数 + 全部', catButtons === tree.categories.length + 1, `${catButtons} vs ${tree.categories.length + 1}`)
    await shot(page, '01-home.png', true)

    const catName = tree.categories.find((cat) => cat.count > 1 && cat.count < tree.total)?.name || tree.categories[0].name
    await page.click(`#kb-cats .kb-cat[data-cat="${catName}"]`)
    await page.waitForTimeout(400)
    const catApi = (await api('/api/kb?category=' + encodeURIComponent(catName))).json
    const catCards = await page.locator('.kb-item').count()
    check(`分类筛选「${catName}」`, catCards === Math.min(catApi.total, 20),
      `ui=${catCards} api=${catApi.total}`)
    const seriesButton = page.locator('#kb-cats .kb-series[data-series]:not([data-series=""])').first()
    if (await seriesButton.count()) {
      const seriesName = await seriesButton.getAttribute('data-series')
      await seriesButton.click()
      await page.waitForTimeout(400)
      const seriesApi = (await api(`/api/kb?category=${encodeURIComponent(catName)}&series=${encodeURIComponent(seriesName)}`)).json
      const seriesCards = await page.locator('.kb-item').count()
      check(`系列筛选「${seriesName}」`, seriesCards === Math.min(seriesApi.total, 20), `ui=${seriesCards} api=${seriesApi.total}`)
    } else {
      check('系列筛选', false, '该分类没有系列')
    }
    await shot(page, '02-nested.png', true)
    await page.click('#kb-cats .kb-cat[data-cat=""]')
    await page.waitForTimeout(300)

    const dateRow = tree.dates.find((row) => row.count > 0)
    await page.click(`#kb-dates .kb-date[data-date="${dateRow.date}"]`)
    await page.waitForTimeout(400)
    const dateApi = (await api(`/api/kb?date_from=${dateRow.date}&date_to=${dateRow.date}`)).json
    check(`日期筛选 ${dateRow.date}`, (await page.locator('.kb-item').count()) === Math.min(dateApi.total, 20), `api=${dateApi.total}`)
    await page.click('#kb-dates .kb-date[data-date=""]')
    await page.waitForTimeout(300)

    // 自定义日期区间：填两个日期 → 应用，卡片数应与 API 区间查询一致
    const sortedDates = tree.dates.map((row) => row.date).filter(Boolean).sort()
    const rangeFrom = sortedDates[0]
    const rangeTo = sortedDates[sortedDates.length - 1]
    await page.fill('#kb-from', rangeFrom)
    await page.fill('#kb-to', rangeTo)
    await page.click('.kb-range-go')
    await page.waitForTimeout(500)
    const rangeApi = (await api(`/api/kb?date_from=${rangeFrom}&date_to=${rangeTo}`)).json
    check(`P0 自定义日期区间 ${rangeFrom} → ${rangeTo}`,
      (await page.locator('.kb-item').count()) === Math.min(rangeApi.total, 20), `api=${rangeApi.total}`)
    check('P0 区间筛选出现可清除的筛选条',
      (await page.locator('#kb-filterbar').innerText()).includes('日期：'))
    const badFrom = sortedDates[sortedDates.length - 1]
    const badTo = sortedDates[0]
    await page.fill('#kb-from', badFrom)
    await page.fill('#kb-to', badTo)
    await page.click('.kb-range-go')
    await page.waitForTimeout(300)
    check('P0 起止日期倒置时拒绝筛选并提示', (await page.locator('.bl-toast').innerText()).includes('起始日期'))
    await page.click('#kb-filterbar .kb-chip:has-text("日期：")')
    await page.waitForTimeout(400)
    check('P0 清除区间后恢复全量', (await page.locator('.kb-item').count()) === Math.min(tree.total, 20))

    // ---------- v0.3：标签导航 ----------
    const tagButtons = page.locator('#kb-tags button.kb-tag-chip')
    check('v0.3 左导航渲染标签栏', (await tagButtons.count()) >= 3, `chips=${await tagButtons.count()}`)
    const firstTagBtn = page.locator('#kb-tags button.kb-tag-chip', { hasText: 'A股' }).first()
    const tagName = (await firstTagBtn.getAttribute('data-tag')) || 'A股'
    await firstTagBtn.click()
    await page.waitForTimeout(500)
    const tagApiTotal = ((await api('/api/kb?tag=' + encodeURIComponent(tagName))).json || {}).total
    const tagBarText = await page.locator('#kb-filterbar').innerText()
    check('v0.3 点标签后出现筛选 chip', tagBarText.includes('标签：') && tagBarText.includes(tagName), tagBarText.replace(/\s+/g, ' ').slice(0, 60))
    check('v0.3 标签筛选列表数量与 API 一致',
      (await page.locator('#kb-result').innerText()).includes('/ ' + tagApiTotal), `api=${tagApiTotal}`)
    await firstTagBtn.click()
    await page.waitForTimeout(400)

    // ---------- 检索 ----------
    await page.fill('#kb-search', '铝业')
    await page.waitForTimeout(700)
    const searchApi = (await api('/api/kb?q=' + encodeURIComponent('铝业'))).json
    check('标题搜索「铝业」结果一致且高亮', (await page.locator('.kb-item').count()) === Math.min(searchApi.total, 20) &&
      (await page.locator('.kb-item mark').count()) > 0, `api=${searchApi.total}`)
    const filteredTree = (await api(`/api/kb/tree?q=${encodeURIComponent('铝业')}`)).json
    const filteredAll = (await page.locator('#kb-cats .kb-cat[data-cat=""] .kb-cat-n').innerText()).trim()
    check('筛选态分类计数随搜索收敛', filteredAll === String(filteredTree.total),
      `ui=${filteredAll} api=${filteredTree.total}`)
    await shot(page, '03-search.png', true)

    const bodyQuery = '资产负债率'
    await page.fill('#kb-search', bodyQuery)
    await page.waitForTimeout(700)
    const bodyApi = (await api('/api/kb?q=' + encodeURIComponent(bodyQuery))).json
    const bodyHit = await page.locator('.kb-item mark').count()
    check(`正文检索「${bodyQuery}」有命中且高亮`, bodyApi.total > 0 && bodyHit > 0, `api=${bodyApi.total} marks=${bodyHit}`)
    await page.click('#kb-search-clear')
    await page.waitForTimeout(500)
    check('清空搜索后恢复全量', (await page.locator('.kb-item').count()) === Math.min(tree.total, 20))

    // ---------- 排序 / 分页 ----------
    const titleApi = (await api('/api/kb?sort=title&limit=1')).json
    await page.selectOption('#kb-sort', 'title')
    await page.waitForTimeout(500)
    const firstTitle = (await page.locator('.kb-item-open').first().innerText()).trim()
    check('按标题排序', firstTitle === titleApi.items[0].title, firstTitle.slice(0, 24))
    await page.selectOption('#kb-sort', 'date_desc')
    await page.waitForTimeout(400)

    check('分页器可见', await page.locator('#kb-pager').isVisible())
    const firstPathBefore = await page.locator('.kb-item').first().getAttribute('data-path')
    await page.click('#kb-next')
    await page.waitForTimeout(500)
    const firstPathAfter = await page.locator('.kb-item').first().getAttribute('data-path')
    check('下一页翻页生效', firstPathBefore !== firstPathAfter && (await page.locator('#kb-prev').isEnabled()))
    check('分页信息显示页码范围', /\/ \d+$/.test(await page.locator('#kb-result').innerText()))
    await page.click('#kb-prev')
    await page.waitForTimeout(500)

    // ---------- 详情抽屉 / 已读收藏 ----------
    const unreadBefore = (await api('/api/kb/tree')).json.unread
    const cardTitle = (await page.locator('.kb-item-open').first().innerText()).trim()
    const cardPath = await page.locator('.kb-item').first().getAttribute('data-path')
    await page.click('.kb-item-open >> nth=0')
    await page.waitForSelector('#kb-drawer:not(.bl-hidden)')
    await page.waitForTimeout(600)
    const drawerTitle = (await page.locator('#kb-drawer-title').innerText()).trim()
    const drawerMeta = await page.locator('#kb-drawer-meta').innerText()
    check('抽屉打开并显示标题', drawerTitle === cardTitle,
      `drawer=${drawerTitle.slice(0, 24)} card=${cardTitle.slice(0, 24)}`)
    const kickerTitle = await page.locator('#kb-drawer-kicker .kb-badge').getAttribute('title')
    check('抽屉分类徽章标注来源（AI/规则）', /AI 分类|规则分类/.test(kickerTitle || ''), `title=${kickerTitle}`)
    const drawerText = await page.locator('#kb-drawer-body').innerText()
    check('抽屉展示正文摘要（preview）', drawerText.length > 60, `len=${drawerText.length}`)
    check('抽屉「打开原文」链接指向相对路径',
      (await page.locator('#kb-drawer-actions a.kb-btn-primary').getAttribute('href')) === `reports/${cardPath}`)
    const unreadAfter = (await api('/api/kb/tree')).json.unread
    check('打开抽屉自动标记已读', unreadAfter === unreadBefore - 1, `${unreadBefore} → ${unreadAfter}`)
    await shot(page, '04-drawer.png', true)

    await page.click('#kb-drawer-star')
    await page.waitForTimeout(600)
    const starredCount = (await api('/api/kb/tree')).json.starred
    check('抽屉收藏写入后端', starredCount === 1, `starred=${starredCount}`)
    await page.click('.kb-drawer-foot button')
    await page.waitForTimeout(300)

    // ---------- P1：AI 在界面上的呈现 ----------
    await page.locator('#count-all').click()
    await page.waitForTimeout(300)
    check('P1 卡片显示 AI 标记与标签', (await page.locator('.kb-ai-mark').count()) >= 1 && (await page.locator('.kb-item-tags .kb-tag').count()) >= 3,
      `marks=${await page.locator('.kb-ai-mark').count()} tags=${await page.locator('.kb-item-tags .kb-tag').count()}`)
    await page.locator('.kb-item-open').first().click()
    await page.waitForSelector('#kb-drawer:not(.bl-hidden)', { timeout: 5000 })
    check('P1 抽屉展示 AI 摘要块', await page.locator('.kb-ai-block').isVisible())
    check('P1 抽屉展示要点列表', (await page.locator('.kb-points li').count()) >= 3)
    check('P1 抽屉展示标签与实体', (await page.locator('.kb-drawer-tags .kb-tag').count()) >= 3 && (await page.locator('.kb-entity').count()) >= 2)
    await page.locator('.kb-drawer-foot button').click()
    await page.waitForTimeout(200)

    // ---------- P6：公开分享 UI ----------
    const publicCount = (await api('/api/kb/tree')).json.public
    check('P6 左导航出现公开计数', (await page.locator('#count-public').innerText()).trim() === String(publicCount), `${publicCount}`)
    await page.locator('.kb-item-open').first().click()
    await page.waitForSelector('#kb-drawer:not(.bl-hidden)', { timeout: 5000 })
    check('P6 抽屉有公开按钮', (await page.locator('#kb-drawer-public').count()) === 1)
    await page.locator('#kb-drawer-public').click()
    await page.waitForTimeout(900)
    const afterPub = (await api('/api/kb/public')).json
    check('P6 抽屉切换公开写入后端', (afterPub?.paths || []).length >= 1, JSON.stringify(afterPub?.paths))
    check('P6 公开后出现复制链接按钮', (await page.locator('#kb-drawer-copy').count()) === 1)
    check('P6 卡片出现公开徽标', (await page.locator('.kb-pub-mark').count()) >= 1)
    await shot(page, '09-public.png', true)
    await page.locator('#kb-drawer-public').click()
    await page.waitForTimeout(900)
    check('P6 取消公开后集合清空', ((await api('/api/kb/public')).json?.paths || []).length === 0)
    await page.locator('.kb-drawer-foot button').click()
    await page.waitForTimeout(300)

    await page.click('.kb-navitem[data-view="starred"]')
    await page.waitForTimeout(500)
    check('收藏视图出现已收藏报告', (await page.locator('.kb-item').count()) >= 1 && await page.locator('.kb-item .kb-star.is-on').count() >= 1)
    await page.click('.kb-navitem[data-view="unread"]')
    await page.waitForTimeout(500)
    const unreadUi = (await api('/api/kb?view=unread')).json.total
    check('未读视图计数一致', (await page.locator('.kb-item').count()) === Math.min(unreadUi, 20), `api=${unreadUi}`)
    await page.click('.kb-navitem[data-view="all"]')
    await page.waitForTimeout(400)

    // ---------- 打开原文 ----------
    await page.click('.kb-item-open >> nth=0')
    await page.waitForSelector('#kb-drawer:not(.bl-hidden)')
    const [reportPage] = await Promise.all([
      context.waitForEvent('page'),
      page.click('#kb-drawer-actions a.kb-btn-primary'),
    ])
    await reportPage.waitForURL(/\/reports\//, { timeout: 20000 }).catch(() => {})
    await reportPage.waitForLoadState('load').catch(() => {})
    const reportUrl = reportPage.url()
    check('打开原文：面板内相对路径能打开报告', reportUrl.includes(`/kb/reports/${cardPath}`), reportUrl)
    check('报告正文无 /mine/ 路径且标题非空',
      !(await reportPage.content()).includes('/mine/') && (await reportPage.title()).trim().length > 0)
    await reportPage.screenshot({ path: join(OUT, '05-report.png'), fullPage: true })
    await reportPage.close()

    // ---------- P5：周报面板 ----------
    await page.keyboard.press('Escape')
    await page.waitForTimeout(200)
    await page.click('#kb-digest-entry')
    await page.waitForSelector('#kb-digest:not(.bl-hidden)')
    await page.waitForSelector('#kb-digest-body .kb-digest-meta', { timeout: 15000 })
    const digestText = await page.locator('#kb-digest-body').innerText()
    check('P5 周报面板展示概览与分类统计',
      digestText.includes('篇报告') && digestText.includes('分类'),
      digestText.slice(0, 60).replace(/\s+/g, ' '))
    check('P5 周报主题卡片或统计视图存在',
      (await page.locator('.kb-theme').count()) > 0 || (await page.locator('.kb-digest-chip').count()) > 0)
    await shot(page, '08-digest.png', true)
    await page.click('#kb-digest .kb-ask-foot .kb-btn-primary')
    await page.waitForTimeout(200)
    check('P5 关闭周报回到列表', await page.locator('#kb-digest').evaluate((node) => node.classList.contains('bl-hidden')))

    // ---------- 批量操作（P7） ----------
    await page.click('.kb-navitem[data-view="all"]')
    await page.waitForTimeout(400)
    const batchBefore = (await api('/api/kb/tree')).json
    await page.click('#kb-select-toggle')
    await page.waitForSelector('.kb-check')
    check('P7 批量模式出现勾选框', (await page.locator('.kb-check').count()) > 0)
    await page.locator('.kb-check').nth(0).check()
    await page.locator('.kb-check').nth(1).check()
    check('P7 批量条显示已选数量', (await page.locator('#kb-batch-count').innerText()).trim() === '已选 2 篇',
      await page.locator('#kb-batch-count').innerText())
    const selPaths = await page.$$eval('.kb-check:checked', (els) => els.map((el) => el.dataset.path))
    await page.click('[data-batch="star"]')
    await page.waitForTimeout(1200)
    const starredItems = (await api('/api/kb?view=starred&limit=500')).json.items.map((item) => item.path)
    const batchAfter = (await api('/api/kb/tree')).json
    check('P7 批量收藏写入后端', selPaths.every((path) => starredItems.includes(path)) &&
      batchAfter.starred >= batchBefore.starred + 1,
      `${batchBefore.starred} → ${batchAfter.starred}，选中 ${selPaths.length} 篇`)
    check('P7 批量操作后清空勾选', (await page.locator('.kb-check:checked').count()) === 0)
    await page.click('#kb-select-toggle')
    await page.waitForTimeout(300)

    // ---------- 搜索历史（P7） ----------
    await page.fill('#kb-search', '铝业')
    await page.waitForTimeout(1000)
    await page.fill('#kb-search', '')
    await page.waitForTimeout(500)
    check('P7 搜索历史记录了刚搜过的词', (await page.locator('.kb-hist-item').count()) >= 1,
      `${await page.locator('.kb-hist-item').count()} 条`)
    await shot(page, '14-history.png', true)

    // ---------- 已保存筛选（P7） ----------
    await page.click('#kb-save-filter')
    await page.waitForTimeout(500)
    check('P7 保存筛选出现在左导航', (await page.locator('.kb-saved-item').count()) === 1,
      await page.locator('#kb-saved').innerText())
    await page.locator('.kb-saved-item').first().click()
    await page.waitForTimeout(900)
    check('P7 套用已保存筛选后列表正常', (await page.locator('.kb-item').count()) > 0)
    await shot(page, '15-saved-filter.png', true)
    await page.locator('.kb-saved-del').first().click()
    await page.waitForTimeout(300)
    check('P7 删除保存筛选后列表消失', (await page.locator('.kb-saved-item').count()) === 0)

    // ---------- 移动端 ----------
    await page.setViewportSize({ width: 390, height: 844 })
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForSelector('.kb-item')
    check('移动端：侧栏与卡片可见', await page.locator('.kb-side').isVisible() && await page.locator('.kb-item').first().isVisible())
    await page.fill('#kb-search', '全景')
    await page.waitForTimeout(700)
    check('移动端：搜索可用', (await page.locator('.kb-item').count()) > 0)
    await shot(page, '06-mobile.png', true)

    check('主页面无 JS 报错 / console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  } catch (error) {
    check('验收流程未抛异常', false, error && error.stack ? error.stack.split('\n')[0] : String(error))
  } finally {
    try { if (browser) await browser.close() } catch {}
    await stopServer(child)
    if (DATA_DIR) rmSync(DATA_DIR, { recursive: true, force: true })
  }

  const passed = results.filter((item) => item.ok).length
  const failed = results.length - passed
  console.log(`\n${passed}/${results.length} passed; artifacts: ${OUT}`)
  for (const item of results.filter((entry) => !entry.ok)) console.log(`FAILED: ${item.name}${item.detail ? ` — ${item.detail}` : ''}`)
  process.exit(failed === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('[verify] 未捕获异常:', error)
  process.exit(1)
})
