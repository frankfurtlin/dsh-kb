#!/usr/bin/env node
/**
 * dsh-kb 在真实 DSH Web GUI 里的验收（Playwright + chromium headless）。
 *
 * 覆盖：侧栏「知识库」行 → 主面板 iframe /kb/ → 新 UI（左导航计数、卡片、搜索高亮、
 * 分类筛选、详情抽屉、自动已读）→ 本插件相关 console error 为空 → 截图留档。
 *
 * 用法：
 *   TOKEN=$(journalctl -u deepseek-harness --no-pager --since today | grep -o 'token=[A-Za-z0-9_-]*' | tail -1)
 *   node verification/verify-dsh.mjs --url "http://127.0.0.1:3080/?$TOKEN"
 */
import { createRequire } from 'node:module'
import { mkdir } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

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

const args = process.argv.slice(2)
function arg(name, fallback) {
  const index = args.indexOf(`--${name}`)
  return index === -1 ? fallback : args[index + 1] ?? fallback
}
const URL = arg('url')
if (!URL) {
  console.error('缺少 --url "http://127.0.0.1:3080/?token=..."')
  process.exit(2)
}
const OUT = arg('out', join(dirname(fileURLToPath(import.meta.url)), 'shots'))
await mkdir(OUT, { recursive: true })

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok: Boolean(ok), detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`)
}
const pluginErrorPattern = /(dsh-kb|\/kb\/)/i

const browser = await launchBrowser()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
const consoleErrors = []
page.on('console', (message) => {
  if (message.type() !== 'error') return
  const location = message.location()?.url || ''
  if (pluginErrorPattern.test(location)) consoleErrors.push(`${message.text()} @ ${location}`)
})
page.on('requestfailed', (request) => {
  const reason = (request.failure() && request.failure().errorText) || ''
  // 导航/切面板会主动取消在途 fetch（net::ERR_ABORTED），不算插件错误
  if (reason.includes('ERR_ABORTED')) return
  if (pluginErrorPattern.test(request.url())) consoleErrors.push(`requestfailed ${request.url()} ${reason}`)
})

async function waitForFrame() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const frame = page.frames().find((candidate) => candidate.url().includes('/kb/'))
    if (frame) {
      try {
        await frame.waitForSelector('.kb-item', { timeout: 1500 })
        return frame
      } catch {}
    }
    await page.waitForTimeout(250)
  }
  return null
}

try {
  check('匿名 /kb/ 被 DSH 拦下', (await context.request.get('http://127.0.0.1:3080/kb/')).status() === 401)

  await page.goto(URL, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(600)

  const kbRow = page.locator('[data-dsh-panel-entry="kb"], .sidebar-panel-row, button:has-text("知识库")').first()
  await kbRow.waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {})
  check('侧栏出现「知识库」', await kbRow.isVisible().catch(() => false))
  await kbRow.click()
  await page.waitForTimeout(2000)

  const frameElement = page.locator('iframe[data-dsh-kb-frame]').first()
  check('主面板挂载 k b iframe',
    (await frameElement.count()) > 0 && (await frameElement.getAttribute('src')) === '/kb/',
    await frameElement.getAttribute('src'))

  const frame = await waitForFrame()
  check('iframe 内渲染知识库新 UI', Boolean(frame))
  if (frame) {
    check('iframe URL 为 /kb/ 前缀', frame.url().includes('/kb/'), frame.url())
    const cards = await frame.locator('.kb-item').count()
    const tree = await frame.evaluate(() => fetch('api/kb/tree').then((response) => response.json()))
    check('首屏卡片数与 total 一致', cards === Math.min(tree.total, 20), `cards=${cards} total=${tree.total}`)
    check('左导航计数渲染', (await frame.locator('#count-all').innerText()) === String(tree.total),
      await frame.locator('#count-all').innerText())
    const cats = await frame.locator('#kb-cats .kb-cat').count()
    check('分类列表渲染', cats === tree.categories.length + 1, `${cats} vs ${tree.categories.length + 1}`)

    await frame.fill('#kb-search', '铝业')
    await page.waitForTimeout(900)
    const marks = await frame.locator('.kb-item mark').count()
    check('iframe 内搜索「铝业」并有高亮', marks > 0, `marks=${marks}`)
    await frame.click('#kb-search-clear')
    await page.waitForTimeout(600)

    await frame.click('#kb-cats .kb-cat[data-cat="个股分析"]')
    await page.waitForTimeout(700)
    const catCards = await frame.locator('.kb-item').count()
    check('iframe 内分类筛选「个股分析」', catCards > 0 && catCards <= 20, `cards=${catCards}`)
    await frame.click('#kb-cats .kb-cat[data-cat=""]')
    await page.waitForTimeout(600)

    const unreadBefore = Number(await frame.locator('#count-unread').innerText())
    await frame.click('.kb-item.is-unread .kb-item-open >> nth=0')
    await frame.waitForSelector('#kb-drawer:not(.bl-hidden)')
    await page.waitForTimeout(700)
    const drawerTitle = (await frame.locator('#kb-drawer-title').innerText()).trim()
    const drawerText = await frame.locator('#kb-drawer-body').innerText()
    check('iframe 内详情抽屉打开且有内容', drawerTitle.length > 0 && drawerText.length > 60,
      `${drawerTitle.slice(0, 20)} len=${drawerText.length}`)
    const unreadAfter = Number(await frame.locator('#count-unread').innerText())
    check('抽屉打开自动已读', unreadAfter === unreadBefore - 1, `${unreadBefore} → ${unreadAfter}`)
    await page.screenshot({ path: join(OUT, '07-dsh-kb-panel.png'), fullPage: true })
    await frame.click('.kb-drawer-foot button')
    await page.waitForTimeout(300)

    // P6：公开分享（匿名只读外链）—— 状态自适应：按当前按钮 aria-pressed 决定断言方向
    await frame.click('.kb-item-open >> nth=0')
    await frame.waitForSelector('#kb-drawer:not(.bl-hidden)')
    await page.waitForTimeout(500)
    check('抽屉有公开分享按钮', (await frame.locator('#kb-drawer-public').count()) === 1)
    const pressed = (await frame.getAttribute('#kb-drawer-public', 'aria-pressed')) === 'true'
    if (pressed) {
      // 当前已公开：先取消，验证复制按钮消失
      await frame.click('#kb-drawer-public')
      await page.waitForTimeout(900)
      check('取消公开后复制按钮消失', (await frame.locator('#kb-drawer-copy').count()) === 0)
      await frame.click('#kb-drawer-public')
      await page.waitForTimeout(900)
    } else {
      await frame.click('#kb-drawer-public')
      await page.waitForTimeout(900)
    }
    check('公开后出现复制链接按钮', (await frame.locator('#kb-drawer-copy').count()) === 1)
    await page.screenshot({ path: join(OUT, '11-dsh-public.png'), fullPage: true })
    // 复位：取消公开，验证回到未公开态
    await frame.click('#kb-drawer-public')
    await page.waitForTimeout(900)
    check('取消公开后复制按钮消失', (await frame.locator('#kb-drawer-copy').count()) === 0)
    const kbApi = 'http://127.0.0.1:8816'
    const st = await (await fetch(kbApi + '/api/kb/public')).json()
    for (const path of st.paths || []) {
      await fetch(kbApi + '/api/kb/public', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, public: false }),
      })
    }
    await frame.click('.kb-drawer-foot button')
    await page.waitForTimeout(300)

    // P3：AI 问答面板（iframe 内，真实调模型，较慢）
    await frame.click('#kb-ask-entry')
    await frame.waitForSelector('#kb-ask:not(.bl-hidden)', { timeout: 10000 })
    await frame.fill('#kb-ask-input', '最近市场全景报告里有哪些风险信号？')
    await frame.click('#kb-ask-submit')
    await frame.waitForSelector('#kb-ask-result .kb-ask-answer, #kb-ask-result .kb-ask-error', { timeout: 120000 })
    const askText = (await frame.locator('#kb-ask-result').innerText()).trim()
    const citeCount = await frame.locator('#kb-ask-result .kb-ask-cites li').count()
    check('iframe 内 AI 问答返回答案与引用', askText.length > 80 && citeCount > 0,
      `len=${askText.length} cites=${citeCount}`)
    await page.screenshot({ path: join(OUT, '10-dsh-ask.png'), fullPage: true })

    // P5：周报面板（iframe 内）
    await frame.click('#kb-digest-entry')
    await frame.waitForSelector('#kb-digest:not(.bl-hidden)', { timeout: 10000 })
    await frame.waitForSelector('#kb-digest-body .kb-digest-meta', { timeout: 120000 })
    const digestText = await frame.locator('#kb-digest-body').innerText()
    check('iframe 内周报面板展示概览', digestText.includes('篇报告'), digestText.slice(0, 50).replace(/\s+/g, ' '))
    await page.screenshot({ path: join(OUT, '09-dsh-digest.png'), fullPage: true })
    await frame.click('#kb-digest .kb-ask-foot .kb-btn-primary')
    await page.waitForTimeout(300)
  }

  // P12：知识图谱与 LLM Wiki 是两个入口，分别验证（图谱可浏览；Wiki 为占位层）
  await frame.click('#kb-graph-entry')
  await page.waitForTimeout(1500)
  const graphFrame = page.frames().find((item) => item.url().includes('/kb/graph/'))
  const graphTitle = graphFrame ? await graphFrame.title() : ''
  const graphLinks = graphFrame ? await graphFrame.locator('a[href]').count() : 0
  check('iframe 内打开知识图谱页', Boolean(graphFrame) && graphTitle.includes('知识图谱') && graphLinks > 10,
    `title=${graphTitle} links=${graphLinks}`)
  await page.screenshot({ path: join(OUT, '20-dsh-graph.png'), fullPage: true })

  // 回到知识库面板首页，再从同一 iframe 的侧栏进入 LLM Wiki（验证两个入口各自独立）
  if (graphFrame) {
    await graphFrame.goto(`${new globalThis.URL(URL).origin}/kb/`, { waitUntil: 'domcontentloaded' })
    await graphFrame.waitForSelector('#kb-wiki-entry', { timeout: 15000 })
    await graphFrame.click('#kb-wiki-entry')
    await page.waitForTimeout(1500)
    const wikiFrame = page.frames().find((item) => item.url().includes('/kb/wiki/'))
    const wikiText = wikiFrame ? await wikiFrame.locator('body').innerText() : ''
    check('iframe 内 LLM Wiki 占位页说明两层分工',
      Boolean(wikiFrame) && wikiText.includes('LLM Wiki') && wikiText.includes('知识图谱'),
      `len=${wikiText.length}`)
    await page.screenshot({ path: join(OUT, '21-dsh-wiki-placeholder.png'), fullPage: true })
  } else {
    check('iframe 内 LLM Wiki 占位页说明两层分工', false, '知识图谱 iframe 未找到，无法回到面板')
  }
  await page.goto(URL)
  await page.waitForTimeout(600)

  check('本插件相关 console error 为空', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
} catch (error) {
  check('验收流程未抛异常', false, error && error.stack ? error.stack.split('\n')[0] : String(error))
} finally {
  await browser.close()
}

const passed = results.filter((item) => item.ok).length
const failed = results.length - passed
console.log(`\n${passed}/${results.length} passed; artifacts: ${OUT}`)
for (const item of results.filter((entry) => !entry.ok)) console.log(`FAILED: ${item.name}${item.detail ? ` — ${item.detail}` : ''}`)
process.exit(failed === 0 ? 0 : 1)
