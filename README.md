# dsh-kb 「知识库」

把 betterlife 老站的**报告库**拆成独立 DSH 插件：侧栏常驻行「知识库」（`order: 15`，
排在「Automation tasks」之后），主面板内嵌同源 iframe。宿主半把 `/kb/*` 反代到本机
`127.0.0.1:8816` 的 FastAPI；后端只读 betterlife 的 `reports/`，唯一的写入点是自己的
SQLite 索引库（摘要/正文缓存 + 已读/收藏 + AI sidecar + 向量）。

**没有登录/认证代码**：插件不带账号、密码、session、cookie、登录页；鉴权完全交给 DSH
宿主的反代闸门（每个请求先过 `connection.requestRejection()`），后端只监听 `127.0.0.1`。

## 能力（M1 + M2 + AI P1–P5）

**信息架构**
- 左导航：视图（全部 / 未读 / 收藏）→ 分类 → 分类下的系列 → 时间（最近 10 个日期）。
- 分类 8 个；系列按内容派生：个股按「名称(代码)」、话题精读按来源（小红书/公众号/YC/
  AIRP）、Minecraft 按版本阶段（Infdev/Indev/Alpha/Beta/快照/Classic 0.x/1.x 正式版/
  26.x 新版等），共 22 组。
- 列表按日期倒序（或标题/最早/最近更新），20 条一页；卡片显示分类徽章、系列、日期、
  摘要、大小、未读状态，可 star；未读卡片带左侧强调条。

**检索与消费闭环**
- 搜索框同时搜标题与正文（后端 LIKE，标题命中优先），结果高亮 `<mark>`；正文命中返回
  上下文片段；250ms 防抖，`⌘K`/`Ctrl+K` 聚焦，`Esc` 清空或关抽屉。
- 点标题打开右侧**详情抽屉**：分类/系列、标题、日期/大小/路径、正文前 1200 字摘要、
  「打开原文 ↗」「收藏」「标记为未读/已读」；打开抽屉自动标记已读。
- 已读/收藏落在后端 SQLite（`KB_DATA_DIR/kb.sqlite`），刷新/换浏览器不丢；收藏与未读
  视图计数实时联动。

**数据侧**
- 启动时建/增量同步索引：新增/变化（size/mtime 或分类/系列等元数据漂移）才重读正文，
  消失的报告从索引删除；`重新扫描` 按钮强制 refresh。
- 摘要取的正文前 3 个 ≥40 字的 `<p>`，全文去标签后截 20000 字；正文里的 `/mine/*` 与
  根绝对路径在返回时改写成 `/kb/*`。

**AI 知识库（P1–P5）**
- **P1 sidecar**：每篇报告由 **DSH 自己的模型**（`dsh-agent` 默认模型，经宿主
  `lib/ai-bridge.js` 的内部 AI 桥）抽取 `summary_ai / key_points / entities / tags`，写回
  索引库；后端不持有任何模型 API key。卡片优先显示 AI 摘要，抽屉显示
  要点/标签/实体，缺 AI 的卡片有「生成 AI 摘要」按钮。后台线程分批补全（默认 3 篇/30s，
  并发 1，模型限流时自动退避），也可用 CLI 批量跑。
- **P2 语义检索**：本地 `fastembed` + `BAAI/bge-small-zh-v1.5`（512 维，模型在
  `server/data/models`，不依赖在线 embedding）给每篇建向量；搜索默认混合排序
  `0.62·余弦 + 0.38·词法`，工具栏可切「AI 语义 / 关键词」，模型缺席时自动降级关键词。
- **P3 RAG 问答**：侧栏「AI 问答」→ 混合检索 top6 → 只依据检索片段作答，答案带 `[n]`
  引用与相关报告；后端 `POST /api/kb/ask`。
- **P4 DSH 工具**：host 半注册 `kb_search` / `kb_get` / `kb_ask` 三个工具，agent 在对话里
  可直接检索、读报告、问知识库（走本机 8816，不需要登录）。
- **P5 周报**：侧栏「周报」→ 最近 7 天的分类/标签/系列聚合 + 模型主题归纳（3-6 个主题，
  每个带相关报告），结果按天缓存在 `server/data/digest.json`。

## 结构

```
package.json                插件声明（dsh.bundle.patch / dsh.client.platform=web）
cordis.patch.yml            bundle 补丁：insert id=dsh-kb
lib/index.js                host 半：/kb 反代 + DSH 鉴权闸门 + /kb → /kb/ 重定向
lib/tools.js                P4：kb_search / kb_get / kb_ask 三个 DSH 工具
client.js                   client 半：侧栏常驻行「知识库」+ main 面板（工具条 + iframe）
server/app/
  main.py                   FastAPI 组合根（同一批路由挂在 / 与 /kb 两套前缀下）
  config.py                 路径与 AI/向量配置（KB_* / AI_* / EMBED_*）
  data/                     kb.sqlite（索引+向量+状态）、models/（bge-small-zh-v1.5）、digest.json
  services/kb_index.py      报告扫描、分类/系列派生、标题/日期提取、筛选（60s 缓存）
  services/kb_store.py      SQLite 索引：同步、摘要/正文抽取、搜索、已读/收藏
  services/report_html.py   HTML 内链改写（/mine/* → /kb/*，根绝对路径按真实文件重写）
  services/kb_ai.py         AI 客户端（经宿主 /kb/_internal/ai 桥调 DSH 默认模型；JSON 抽取 + 重试）
  services/kb_enrich.py     P1：批量生成 AI 摘要/要点/实体/标签
  services/kb_embed.py      P2：fastembed 本地向量（懒加载；模型/缓存都在 data/ 下）
  services/kb_search.py     P2：混合检索编排 + 向量补齐
  services/kb_ask.py        P3：RAG 问答（检索 → 引用回答）
  services/kb_digest.py     P5：周报 / 主题聚合（带按天缓存）
  cli_enrich.py             批量补 AI sidecar 的 CLI（Hermes 写完报告可调用）
  api/routes/pages.py       GET /（Jinja2 模板）
  api/routes/kb.py          GET /api/kb、/api/kb/tree、/api/kb/search、/api/kb/report/<path>、POST /api/kb/state
  api/routes/reports.py     GET /reports、/reports/、/reports/{path:path}
  web/templates/index.html  知识库页面（左导航 + 列表 + 抽屉）
  web/static/               style.css / core.js / ui.js / kb.js / icon-192.png
verification/verify-app.mjs   后端 + 页面端到端验收（Playwright 直连 8916，自带启动/清理）
verification/verify-proxy.mjs host 半隔离验收（mock ctx，不用装进 DSH）
verification/shots/           验收截图
```

## 安装 / 启动（Lead 执行）

后端是独立 systemd 服务，用**插件内的 venv** `.venv`（系统 python3.11 + fastembed/onnxruntime；
为避免沙箱写权限问题，embeddings 的模型与 HF 缓存都放 `server/data/` 下）。密钥只进
`server/.env.local`（chmod 600），不写进单元文件：

```ini
[Unit]
Description=dsh-kb knowledge base backend
After=network.target

[Service]
WorkingDirectory=/root/workspace/plugins/dsh-kb/server
Environment=KB_REPORTS_DIR=/root/workspace/betterlife/reports
Environment=KB_DATA_DIR=/root/workspace/plugins/dsh-kb/server/data
EnvironmentFile=-/root/workspace/plugins/dsh-kb/server/.env.local
ExecStart=/root/workspace/plugins/dsh-kb/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8816
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

插件挂载由 Lead 用 `plugin_manager` 完成（安装/热挂载后刷新 DSH 页面，侧栏出现
「知识库」，顺序在「Automation tasks」之后、「紫微斗数」之前）：

```
plugin_manager action=install_bundle target=/root/workspace/plugins/dsh-kb
```

host 半默认反代 `127.0.0.1:8816`；如需临时指向其它端口，可设
`DSH_KB_UPSTREAM_PORT`（仅供测试，正式环境不要设）。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KB_REPORTS_DIR` | `/root/workspace/betterlife/reports` | 报告根目录（只读） |
| `KB_DATA_DIR` | `server/data` | 索引库目录；`kb.sqlite` / `digest.json` / `models/` 都在这里 |
| `KB_AI_URL` | `http://127.0.0.1:3080/kb/_internal/ai` | host 插件的内部 AI 桥（调 DSH 默认模型） |
| `KB_AI_SECRET_FILE` | `server/.ai-secret` | 桥共享密钥（host 半首次加载自动生成，0600；缺失时 AI 优雅关闭） |
| `KB_AI_TIMEOUT` / `KB_AI_MAX_TOKENS` | `180` / `2000` | 单次模型调用超时与输出上限 |
| `KB_AI_AUTO` | `1` | 是否起后台线程自动补 AI sidecar |
| `KB_AI_BATCH` / `KB_AI_INTERVAL` / `KB_AI_CONCURRENCY` | `3` / `30` / `1` | 后台每轮篇数、间隔秒、并发 |
| `KB_EMBED_ENABLED` | `1` | 是否启用本地向量检索 |
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | fastembed 模型名 |
| `KB_EMBED_MODEL_DIR` / `KB_EMBED_HF_HOME` | `server/data/models` / `server/data/hf-cache` | 模型与 HF 缓存目录 |
| `KB_HYBRID_WEIGHT_SEMANTIC` / `KB_HYBRID_WEIGHT_LEXICAL` | `0.62` / `0.38` | 混合检索权重 |

删除 `KB_DATA_DIR/kb.sqlite` 即可重建索引（已读/收藏状态会一并清空，属预期）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/kb` | 列表：`q`、`category`、`series`、`date_from`、`date_to`、`view=unread|starred`、`sort=date_desc|date_asc|title|updated`、`offset`、`limit`（≤500）、`refresh=1` → `{items, total, scan_total}`；`q` 命中时 item 附 `snippet`/`match` |
| GET | `/api/kb/tree` | 导航树：`{total, unread, starred, categories, series, dates, scan_total}` |
| GET | `/api/kb/search?q=` | 与列表相同的检索，返回 `{q, items, total}` |
| GET | `/api/kb/search?q=&mode=` | 混合检索：`mode=auto|hybrid|lexical`；返回 `{q, items, total, mode}`，item 带 `score/semantic_score/match/snippet` |
| GET | `/api/kb/report/{path:path}` | 单篇 `{item}`（含 `preview` 前 1200 字、AI 摘要/要点/标签/实体）；不存在 404 |
| GET | `/api/kb/ai/stats` | AI sidecar 统计（`configured/model/total/enriched/failed/pending/version`） |
| POST | `/api/kb/enrich` | body `{path?, limit?, force?, retry_failed?}`：手动补 AI 摘要（≤50/次） |
| GET | `/api/kb/embed/stats` | 向量索引统计（`enabled/available/model/dim/stats`） |
| POST | `/api/kb/embed` | 手动补一批向量（默认 16 篇） |
| POST | `/api/kb/ask` | body `{question, limit?}` → `{ok, answer, citations, related, mode}`（未配置模型 503） |
| GET | `/api/kb/digest?days=7&refresh=0` | 周报：`{ok, total, categories, series, tags, themes, overview, mode, cached}` |
| POST | `/api/kb/state` | body `{path, read?, starred?}`，路径校验（禁止 `..`、只允许 .html/.htm） |
| GET | `/reports`、`/reports/{path:path}` | 报告正文；HTML 改写内链后返回 |

上述路径同时挂在 `/kb/*` 下（如 `/kb/`、`/kb/api/kb`、`/kb/reports/...`、`/kb/static/...`），
因为 host 半按 ziwei 的惯例**原样转发** `req.url`（不剥前缀）。直连 8916 时两套路径都可用，
iframe 内使用 `/kb/` 这一套。

## 验收

两条命令都是自包含的：自己拉起/清理 8916 上的 uvicorn，占用 8916 时直接报错退出。
`verify-app.mjs` 会把 `KB_DATA_DIR` 指向临时目录，不污染生产索引。

```bash
cd /root/workspace/plugins/dsh-kb/verification
node verify-app.mjs      # 后端 + 页面端到端（Playwright 直连 http://127.0.0.1:8916）
node verify-proxy.mjs    # host 半（lib/index.js）隔离验收，不需要装进 DSH
```

最近一次结果（2026-11-05，M1+M2+AI 后）：

- `verify-app.mjs` **47/47 passed** —— 树/列表/检索/前缀反代/重定向；分类、系列、日期筛选；
  标题与正文搜索高亮；排序与分页；详情抽屉（preview、自动已读、收藏、打开原文）；
  AI sidecar 卡片/抽屉；向量与问答接口的降级路径；周报面板（概览/主题/统计）；收藏/未读
  视图；移动端；无 JS 报错。
- `verify-proxy.mjs` **11/11 passed** —— 导出与注册、`/kb/`、`/kb/api/kb`、`/kb/static/*`、
  AI 桥鉴权（无密钥/错密钥都按 404 隐藏）
  反代、`/kb?x=1 → 302 /kb/?x=1`、401/403 鉴权闸门、上游未启动时 502 中文提示。
- `verify-dsh.mjs` **15/15 passed**（真实 DSH 面板内）—— 匿名 401、侧栏「知识库」行、
  iframe `/kb/`、首屏卡片/计数/分类、搜索高亮、分类筛选、详情抽屉与自动已读、AI 问答
  （真实调模型，答案 + 引用）、周报面板、无插件相关 console error。

AI 问答与周报要调模型（单次 20–40s），host 半对 `/kb/api/kb/ask`、`/kb/api/kb/digest`
单独放宽到 180s，其余接口仍 10s 超时。

截图在 `verification/shots/`：

| 文件 | 内容 |
| --- | --- |
| `01-home.png` | 首屏：左导航（视图/分类/时间）+ 卡片列表 |
| `02-nested.png` | 分类展开系列（Minecraft 版本 → 1.x 正式版 等） |
| `03-search.png` | 搜索命中与高亮 |
| `04-drawer.png` | 详情抽屉（摘要、打开原文、收藏、已读） |
| `05-report.png` | 打开原文的报告正文页 |
| `06-mobile.png` | 390×844 移动端 |
| `07-dsh-kb-panel.png` | 真实 DSH 面板内（侧栏 → 知识库 → iframe → 抽屉），由 `verify-dsh.mjs` 产出 |
| `08-digest.png` | 周报面板（窗口、主题卡片、分类/标签统计） |
| `09-dsh-digest.png` | 真实 DSH 面板内的周报视图（`verify-dsh.mjs`） |
| `10-dsh-ask.png` | 真实 DSH 面板内的 AI 问答（答案 + 引用，`verify-dsh.mjs`） |

语法门：`node --check lib/index.js && node --check client.js && node --check verification/verify-app.mjs && node --check verification/verify-proxy.mjs`；
Python 用 `/usr/bin/python3.11 -m py_compile $(find server -name '*.py')`。

## 实现要点

- **相对路径**：模板引用 `static/style.css`、`static/kb.js`，`core.js` 里 `API = 'api'`，
  报告 URL 为 `reports/<rel>`，因此页面在 `/` 与 `/kb/` 下都能工作；iframe 内 base 为 `/kb/`。
- **报告内链改写**（`report_html.py`）：先把 `/mine/reports/`、`/mine/report/`、`/mine/static/`、
  `/mine/` 换成 `/kb/...`，再把 HTML 里根绝对的 `href`/`src`/`url(...)` 中**确实存在于
  `reports/` 的目标**改写成 `/kb/reports/...`；不存在的目标保持原样，避免把外链改坏。
- **索引而不是每次扫描**：`kb_store.sync()` 用 `(size, mtime)` + 元数据判断增量；正文与摘要
  只在变化时重读。SQLite 开 WAL + `busy_timeout` + `temp_store=MEMORY`（受限沙箱里避免
  大 LIMIT 查询需要临时文件）。
- **搜索**：标题与正文都走 LIKE（转义 `%`/`_`），标题命中优先；正文命中给前后 80 字片段。
- **AI sidecar**：`kb_ai.chat_json()` 统一 `thinking.type=disabled` + `response_format=json_object`
  + 进程内节流与 429 长退避；`kb_enrich` 首次失败后用更短正文 + temperature 0 重试一次，
  仍失败记 `ai_error`，不影响索引和检索。
- **向量**：进程内懒加载 fastembed；`document_text()` = 标题 + AI 摘要/摘要 + 标签 + 正文前
  2000 字；向量以 float32 BLOB 存在 SQLite，查询时全量余弦（460 篇规模足够快）。
- **周报**：只把窗口内报告清单喂给模型，主题里引用的路径必须来自素材白名单；模型不可用时
  退化成纯统计视图；结果按天缓存，`refresh=1` 重算。
- **安全**：所有请求先过 DSH 鉴权；`safe_path()` 用 `resolve()` + `relative_to()` 防目录穿越；
  POST 状态接口只接受 .html/.htm 且拒绝 `..`；后端只监听 `127.0.0.1`；不暴露 docs/openapi。

## 已知限制 / 风险

- 侧栏行与 main 面板依赖宿主 `sidebar.panellist` / `main` 座位与 `ctx.layout.selectPanel(null)`
  的现有语义（与 dsh-tavern、dsh-bartending 相同）；宿主大版本升级改动座位时需同步 `client.js`。
- 插件安装后的 DSH 内真实点击验收（侧栏行、面板工具条、iframe 内检索/抽屉）属于 Lead 的
  安装步骤；本包内已用 `verify-proxy.mjs` 覆盖 host 半、用 `verify-app.mjs` 覆盖后端与页面，
  `client.js` 的槽位注册只有装进 DSH 才能最终确认。
- AI 依赖 DSH 当前选中的默认模型（`agentDefaultModel`，由用户在 DSH 里切换）：模型不可用/
  超时会自动退避并把该篇标 `ai_error`，后台线程继续补其它篇；`POST /api/kb/enrich
  {retry_failed:true}` 可重试失败篇。模型额度跟随 DSH 账号，不再单独维护第三方 key。
- AI 桥只在回环地址上、凭 `server/.ai-secret` 接受请求；DSH 未加载本插件时后端 AI 会自动
  降级（问答返回结构化错误、周报退化为统计视图），浏览/检索/已读收藏不受影响。
- 向量检索用本地 bge-small-zh-v1.5（CPU），首次加载约 10s；模型缺失/`KB_EMBED_ENABLED=0`
  时自动降级为关键词检索，接口不报错。
- P4 工具（`kb_search/kb_get/kb_ask`）要在 DSH 重新加载插件后才出现在工具列表；注册本身
  在 `lib/tools.js`，已用 DSH 的 `assertSupportedJsonSchema` 与真实后端校验。
- 周报目前按自然日窗口（默认最近 7 天）取报告日期，不按生成时间；无 AI 摘要的老报告只能
  贡献标题/统计。
- 报告日期来自目录名 `YYYYMMDD/`；无日期目录的报告不参与日期筛选（沿用原站规则）。
- 报告正文里的站外链接、`/login.html` 等不存在的目标保持原样，点击可能在 iframe 内 404
  或跳出 DSH 路由；这是报告自身内容，插件不做兜底。
