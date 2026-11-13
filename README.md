# dsh-kb 「知识库」

把 betterlife 老站的**报告库**拆成独立 DSH 插件：侧栏常驻行「知识库」（`order: 15`，
排在「Automation tasks」之后），主面板内嵌同源 iframe。宿主半把 `/kb/*` 反代到本机
`127.0.0.1:8816` 的 FastAPI；后端只读 betterlife 的 `reports/`，唯一的写入点是自己的
SQLite 索引库（摘要/正文缓存 + 已读/收藏 + 公开标记 + AI sidecar + 向量）。

另有一条**匿名只读外链**（P6）：作者把某篇报告标记为「公开」后，别人不需要 DSH 账号，
就能通过 `https://hermes.frankfurtlin.top/kb-public/...` 打开这篇报告与它引用的附件。

**没有登录/认证代码**：插件不带账号、密码、session、cookie、登录页；鉴权完全交给 DSH
宿主的反代闸门（每个请求先过 `connection.requestRejection()`），后端只监听 `127.0.0.1`。

## 设计目标（产品北极星）

> 「知识库就是一个存放知识的，我给你各种各样的知识，你帮我按照合理的、优雅的、准确的分类，
> 并构建一些知识的 tag 分类之类的，同时后续也可以构建知识图谱、LLM wiki 等关联性，
> 我查询搜索的时候也能方便快捷准确地检索出来。」—— 用户对该插件的定位（2026-11-13）

一句话：**用户只负责投喂知识，AI 负责组织；用户的全部操作只有「放进来」和「查出来」。**

由此拆成四个可验收的目标：

| # | 目标 | 含义 | 现状 |
|---|---|---|---|
| G1 | 自动分类 | 报告进来就归到合理的分类/系列，不需要人工建文件夹、改名 | 规则推导 + HTML 覆盖，半自动（R1 要改成 AI 主导） |
| G2 | 自动标签 | 每篇有稳定、人可读、可筛选、可聚合的主题标签；同义标签自动合并 | AI 已生成 tags/entities 并展示，但还不是导航维度 |
| G3 | 关联 | 报告之间、主题之间有可解释的关系（相关报告 / 知识图谱 / LLM wiki 主题页） | 仅标签/实体软关联，无图谱与主题页 |
| G4 | 准确检索 | 关键词、语义、问答三条路都能快速命中，且质量可度量 | 混合检索已上线，但还没有评测集 |

**术语**（README 其余部分沿用）：

- **报告**：`reports/` 下的一篇 HTML，是知识的最小单位，也是唯一的事实源。
- **分类**：一级主题（当前 8 类）；**系列**：分类下的项目/版本线（当前 22 组）。
- **标签**：AI 从正文抽出的自由词（如 `#A股收盘`、`#半导体`），跨分类可复用。
- **实体**：结构化名词（公司、产品、人物、指标），带类型，是未来图谱的节点。
- **主题页**（规划中）：把同一标签/实体下的所有报告聚成一篇可读的 LLM wiki 页，报告之间用双链引用。

### 演进路线（按依赖顺序）

- **R1 AI 分类**（对应 G1）：`kb_enrich` 在生成摘要时顺带产出 `category_ai` / `series_ai`
  （限定在分类枚举 + 「其他」内），写入索引；现有规则只作兜底与冷启动，分类优先级为
  「人工覆盖 > AI > 规则」。验收：抽样 50 篇人工判定，AI 分类准确率 ≥ 90%。
- **R2 标签一等公民**（G2）：标签规范化（单复数、别名、中英同义合并）、标签页（点标签看
  该标签下所有报告）、标签作为筛选维度与检索加权；验收：标签覆盖率 ≥ 95%，只出现一次的
  碎片标签占比 ≤ 30%。
- **R3 关系与图谱**（G3）：以「报告 – 标签 – 实体」建边，报告详情先出「相关报告」
  （共享标签/实体 + 向量近邻），再考虑可视化成图谱；验收：相关报告 top3 人工相关率 ≥ 70%。
- **R4 LLM Wiki 主题页**（G3）：按高频标签/实体自动生成主题页（综述 + 时间线 + 引用报告），
  支持增量更新；验收：每个高频标签都有可读主题页。
- **R5 检索质量回归**（G4）：建 50–100 条「query → 期望报告」评测集，纳入 pytest 回归，
  记录 Recall@5 / MRR；验收：任何改动不得让指标下降。

### 设计原则

1. **索引永远是派生物**：分类、标签、关系、向量都能从 `reports/` 重建，不产生「只存在于
   索引里、丢了就找不回」的知识。
2. **可解释、可覆盖**：每条 AI 结论保留来源（模型、时间、原文片段），允许人工覆盖且覆盖优先。
3. **增量优先、成本可控**：新报告只跑增量；全量重算由显式命令触发（`refresh` / CLI），
   不在用户查询路径上调用模型。

## 一篇报告从「投喂」到「被查出来」

```
reports/YYYYMMDD/xxx.html
   │ ① 扫描：标题 / 分类 / 系列 / 日期
   ▼
SQLite kb.sqlite ── ② 正文摘要 + 全文（≤2 万字）
   │ ③ AI 增强：摘要 / 要点 / 实体 / 标签（DSH 默认模型）
   │ ④ 向量化：本地 bge-small-zh（512 维）
   ▼
⑤ 组织与检索：视图 / 分类→系列 / 时间 / 关键词 / 语义 / 问答 / 周报
   ▼
⑥ 输出：抽屉预览、打开原文、公开外链、DSH 工具（kb_search/kb_get/kb_ask）
```

| 步骤 | 代码位置 |
|---|---|
| 扫描与分类/系列派生 | `server/app/services/kb_index.py`（`scan()` / `classify()` / `_series_for()`） |
| 增量入库与正文抽取 | `server/app/services/kb_store.py`（`sync()` / `_extract_text()`） |
| AI 增强 | `server/app/services/kb_enrich.py` + `kb_ai.py`（经 `lib/ai-bridge.js` 走 DSH 模型） |
| 向量化与混合排序 | `server/app/services/kb_embed.py` + `kb_store.hybrid_search()` |
| 检索 / 问答 / 周报 | `server/app/services/kb_search.py` / `kb_ask.py` / `kb_digest.py` |
| 前端 | `server/app/web/{templates/index.html, static/kb.js, static/style.css}` |

## 能力（M1 + M2 + AI P1–P5 + P6 公开 + P7 操作效率）

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
- **P6 公开外链**：抽屉里点「公开」→ 生成匿名可访问的
  `https://hermes.frankfurtlin.top/kb-public/reports/<path>` 并复制链接；侧栏「公开」视图
  集中列出已公开报告，卡片带绿色「公开」徽标，再次点击即可取消。
  - 匿名端只放行：已公开报告页本身 + `reports/assets/` 共享资源 + 该报告 HTML/CSS 显式引用
    的附件（css 追一层 `@import/url()`，上限 200）+ 同目录非 HTML 兄弟文件 +
    `<报告目录>/images/<报告名>/` 与 `<报告目录>/<报告名>/` 图片目录；其余一律 404。
  - 只允许 GET/HEAD；写接口 `/api/kb/public` 仍只经 DSH 反代（要登录）。
  - 公开页内链统一改写成 `/kb-public/...`，并带 `X-Robots-Tag: noindex, nofollow`。
  - 外链入口在 DSH 之外（hermes 站点 Caddy），因为 DSH 在路由分发前有全局匿名闸门。

**工程化对齐（P0 / P1）**
- **自定义日期区间**：侧栏「时间」下有起止两个 `date` 输入 + 「应用」，与老站的
  `#kb-from/#kb-to` 等价；只填一边时是半开区间，起晚于止会 toast 提示拒绝；筛选条显示
  `日期：起 → 止` 并可一键清除。快捷日期按钮仍然保留。
- **静态资源版本与缓存**：模板给 css/js/icon 统一拼 `?v=<ASSET_VERSION>`（静态文件
  名+mtime+大小的短 hash），带指纹的响应 `Cache-Control: public, max-age=31536000, immutable`；
  不带指纹的只缓存 5 分钟，避免升级后长期拿到旧文件。HTML 本身 `no-cache`。
- **健康检查**：`GET /healthz`（页面内也有 `/kb/healthz`）返回 `{ok, version, uptime_seconds,
  reports:{total,unread,starred,public}, ai:{configured}, embed:{enabled,available}}`；
  索引读不出来时返回 503 并带 `error`，不触发模型加载。
- **统一错误体**：框架自产的 404/405/422 与业务 `HTTPException` 都收敛成
  `{"ok": false, "error": "..."}`，前端只解析一种形状（报错定位到具体参数名）。
- **压缩与长缓存**：`GZipMiddleware(minimum_size=500)`；静态资源的缓存头见上一条。

**细节打磨（P2 部分）**
- **筛选态计数**：`/api/kb/tree` 接受 `q/category/date_from/date_to/view`，左导航的分类
  数字随搜索/日期/视图收敛（老站行为），时间列表跟随关键词但不按日期过滤；选中分类时
  只统计该分类下的系列。前端在搜索、日期、视图、分类变化时都会重新拉树。
- **观测性**：每个响应带 `X-Process-Time`（毫秒）；超过 `KB_SLOW_REQUEST_MS`（默认 300ms）
  记一条 `[slow]` warning 日志，便于排查「面板卡住」。
- **老站公开状态迁移**：`python -m app.cli_import_kb_public`（可 `--dry-run`）把
  `betterlife/data/kb_public.json` 里 `public: true` 且仍存在的报告导入 SQLite，幂等；
  已用它恢复了老站原有的 3 篇公开报告。

**P7：批量操作与筛选记忆**
- **批量操作**：工具栏「选择」进入多选，卡片出现勾选框；批量条支持全选本页、标记已读、
  收藏、公开、取消公开、复制公开链接、清空。后端 `POST /api/kb/bulk` 一次最多 500 篇，
  重复路径去重，非法路径忽略并计入 `missing`。
- **搜索历史**：搜索过的词存 `localStorage`（最多 8 条），聚焦空搜索框时出现下拉，可点选
  复搜或删除单条。
- **保存筛选**：工具栏「保存筛选」把当前 q/分类/系列/日期区间/视图/排序/检索方式存成一
  条命名快照（最多 10 条），左导航「已保存」里一键套用或删除。

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
  services/kb_public.py     P6：公开标记 + 匿名可访问附件白名单
  cli_enrich.py             批量补 AI sidecar 的 CLI（Hermes 写完报告可调用）
  cli_import_kb_public.py   老站公开状态迁移 CLI（幂等，支持 --dry-run）
  api/routes/pages.py       GET /（Jinja2 模板）
  api/routes/kb.py          GET /api/kb、/api/kb/tree、/api/kb/search、/api/kb/report/<path>、POST /api/kb/state
  api/routes/reports.py     GET /reports、/reports/、/reports/{path:path}
  api/routes/public_site.py P6：匿名只读站 /kb-public/ 与 /kb-public/reports/<path>
  web/templates/index.html  知识库页面（左导航 + 列表 + 抽屉）
  web/static/               style.css / core.js / ui.js / kb.js / icon-192.png
  tests/                    pytest：索引/检索/状态、批量、公开外链与老站迁移、P0/P1 基础设施（35 项）
requirements.txt            后端依赖（fastapi/uvicorn/jinja2/httpx/numpy/fastembed/onnxruntime）
requirements-dev.txt        开发依赖（pytest）
pytest.ini                  pytest 配置（testpaths=tests）
verification/verify-app.mjs   后端 + 页面端到端验收（Playwright 直连 8916，自带启动/清理）
verification/verify-proxy.mjs host 半隔离验收（mock ctx，不用装进 DSH）
verification/verify-dsh.mjs   真实 DSH 面板内验收（Playwright + launch token）
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
| `KB_PUBLIC_BASE_URL` | `https://hermes.frankfurtlin.top/kb-public` | 公开外链前缀（与 Caddy 路由一致；改域名时同步改） |

删除 `KB_DATA_DIR/kb.sqlite` 即可重建索引（已读/收藏状态会一并清空，属预期）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/kb` | 列表：`q`、`category`、`series`、`date_from`、`date_to`、`view=unread|starred`、`sort=date_desc|date_asc|title|updated`、`offset`、`limit`（≤500）、`refresh=1` → `{items, total, scan_total}`；`q` 命中时 item 附 `snippet`/`match` |
| GET | `/api/kb/tree` | 导航树：`{total, unread, starred, public, categories, series, dates, scan_total}`；接受 `q/category/date_from/date_to/view`，计数随筛选收敛 |
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
| GET | `/healthz` | 健康检查：`{ok, version, uptime_seconds, reports, ai, embed}`（503 表示索引不可读） |
| GET | `/api/kb/public` | 公开状态：`{ok, base, paths}` |
| POST | `/api/kb/public` | body `{path, public}`：公开/取消公开 → `{ok, path, public, url}` |
| GET | `/reports`、`/reports/{path:path}` | 报告正文；HTML 改写内链后返回 |
| GET | `/kb-public/`、`/kb-public/reports/{path:path}` | **匿名只读**公开站与公开报告；未公开一律 404 |

上述路径同时挂在 `/kb/*` 下（如 `/kb/`、`/kb/api/kb`、`/kb/reports/...`、`/kb/static/...`），
因为 host 半按 ziwei 的惯例**原样转发** `req.url`（不剥前缀）。直连 8916 时两套路径都可用，
iframe 内使用 `/kb/` 这一套。

## 验收

两条命令都是自包含的：自己拉起/清理 8916 上的 uvicorn，占用 8916 时直接报错退出。
`verify-app.mjs` 会把 `KB_DATA_DIR` 指向临时目录，不污染生产索引。

```bash
# 单元/接口测试（自带临时 reports + 临时索引库，不碰生产数据）
cd /root/workspace/plugins/dsh-kb/server
../.venv/bin/python -m pytest        # 35 passed

# 端到端验收
cd /root/workspace/plugins/dsh-kb/verification
node verify-app.mjs      # 后端 + 页面端到端（Playwright 直连 http://127.0.0.1:8916）
node verify-proxy.mjs    # host 半（lib/index.js）隔离验收，不需要装进 DSH
```

最近一次结果（2026-11-12，P0/P1 工程化对齐后）：

- `pytest` **35 passed** —— 索引同步与分类/系列/日期派生、列表筛选（分类/系列/日期区间/
  半开区间/分页/排序）、标题与正文检索（snippet/match）、详情 preview、已读/收藏往返、
  非法路径 400、批量读写、筛选态树计数、公开外链（外链生成、匿名 200、内链改写、附件白名单、
  目录页、越权 404、取消公开立即生效、老站公开状态迁移）、P0/P1（指纹缓存、gzip、
  统一错误体、healthz、版本号、X-Process-Time）。
- `verify-app.mjs` **82/82 passed** —— 树/列表/检索/前缀反代/重定向；分类、系列、快捷日期
  与**自定义日期区间**（含倒置拒绝、筛选条、清除恢复）；健康检查、统一错误体、静态指纹
  长缓存、无指纹短缓存、gzip；标题与正文搜索高亮；排序与分页；详情抽屉（preview、自动已读、
  收藏、打开原文）；AI sidecar 卡片/抽屉；向量与问答接口的降级路径；周报面板（概览/主题/统计）；
  公开开关（公开/取消公开、公开视图、公开外链匿名 200、附件白名单、私有报告 404、越权路径 404）；
  收藏/未读视图；筛选态分类计数收敛；批量选择与批量收藏；搜索历史；
  保存/套用/删除筛选；移动端；无 JS 报错。
- `verify-proxy.mjs` **11/11 passed** —— 导出与注册、`/kb/`、`/kb/api/kb`、`/kb/static/*`、
  AI 桥鉴权（无密钥/错密钥都按 404 隐藏）
  反代、`/kb?x=1 → 302 /kb/?x=1`、401/403 鉴权闸门、上游未启动时 502 中文提示。
- `verify-dsh.mjs` **18/18 passed**（真实 DSH 面板内；公开开关是状态自适应，初始已公开时会多 1 项）—— 匿名 401、侧栏「知识库」行、
  iframe `/kb/`、首屏卡片/计数/分类、搜索高亮、分类筛选、详情抽屉与自动已读、公开开关
  （抽屉按钮 / 复制链接按钮出现与消失，状态自适应）、AI 问答（真实调模型，答案 + 引用）、
  周报面板、无插件相关 console error。

AI 问答与周报要调模型（单次 20–40s），host 半对 `/kb/api/kb/ask`、`/kb/api/kb/digest`
单独放宽到 180s，其余接口仍 10s 超时。

截图在 `verification/shots/`：

| 文件 | 内容 |
| --- | --- |
| `01-home.png` | 首屏：左导航（视图/分类/时间 + 起止日期输入 + 版本号）+ 卡片列表 |
| `02-nested.png` | 分类展开系列（Minecraft 版本 → 1.x 正式版 等） |
| `03-search.png` | 搜索命中与高亮 |
| `04-drawer.png` | 详情抽屉（摘要、打开原文、收藏、已读） |
| `05-report.png` | 打开原文的报告正文页 |
| `06-mobile.png` | 390×844 移动端 |
| `07-dsh-kb-panel.png` | 真实 DSH 面板内（侧栏 → 知识库 → iframe → 抽屉），由 `verify-dsh.mjs` 产出 |
| `08-digest.png` | 周报面板（窗口、主题卡片、分类/标签统计） |
| `09-dsh-digest.png` | 真实 DSH 面板内的周报视图（`verify-dsh.mjs`） |
| `10-dsh-ask.png` | 真实 DSH 面板内的 AI 问答（答案 + 引用，`verify-dsh.mjs`） |
| `11-dsh-public.png` | 真实 DSH 面板内：抽屉里的「公开 / 已公开」与「复制链接」（`verify-dsh.mjs`） |
| `14-history.png` | 搜索历史下拉（`verify-app.mjs`） |
| `15-saved-filter.png` | 已保存筛选的套用与删除（`verify-app.mjs`） |
| `16-batch.png` | 生产页：批量模式（勾选 3 篇 + 批量条） |
| `17-history-saved.png` | 生产页：搜索历史 + 左导航「已保存」 |

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
  2000 字；向量以 float32 BLOB 存在 SQLite，查询时全量余弦（当前 468 篇规模足够快，千级以内仍可）。
- **周报**：只把窗口内报告清单喂给模型，主题里引用的路径必须来自素材白名单；模型不可用时
  退化成纯统计视图；结果按天缓存，`refresh=1` 重算。
- **安全**：所有请求先过 DSH 鉴权；`safe_path()` 用 `resolve()` + `relative_to()` 防目录穿越；
  POST 状态接口只接受 .html/.htm 且拒绝 `..`；后端只监听 `127.0.0.1`；不暴露 docs/openapi。

## 公开外链部署（hermes 站点 Caddy）

DSH 在路由分发之前有全局匿名闸门（匿名请求 `/kb/` 直接 401），插件无法自己放行匿名访问，
所以公开外链走 DSH 之外的入口：`hermes.frankfurtlin.top` 的 `/kb-public/*` 直连后端
`127.0.0.1:8816`。Caddyfile（`/etc/caddy/Caddyfile`，hermes 站点内）需要：

```caddyfile
handle /kb-public {
    redir * /kb-public/ 308
}
handle /kb-public/* {
    header X-Robots-Tag "noindex, nofollow"
    reverse_proxy 127.0.0.1:8816
}
```

注意：本机 Caddyfile 全局是 `admin off`，`systemctl reload caddy` 会失败（admin API 不可用），
改配置后需 `systemctl restart caddy`（先 `caddy validate --config /etc/caddy/Caddyfile
--adapter caddyfile` 校验；重启会让所有站点短暂断连）。

公开面只回放已公开报告及其附件，`/kb-public/api/*` 之类路径一律 404；公开站带
`X-Robots-Tag: noindex, nofollow`，不被搜索引擎收录。

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
- **分类与系列目前仍是规则推导**（标题关键词 + 路径 + HTML 覆盖），AI 只负责摘要/标签/向量；
  「AI 主导分类」是 R1，尚未实现。
- **标签还不是一等公民**：AI 标签只在卡片/抽屉里展示，不能点标签筛选、没有标签页、不做
  同义合并（R2）。
- **没有跨报告图谱与主题页**：只有「标签/实体」这种软关联，图谱可视化与 LLM wiki 主题页是
  R3/R4。
- **检索质量没有评测集**：目前靠人工抽查与 e2e 通过率，Recall@5 / MRR 回归是 R5。
- 报告正文里的站外链接、`/login.html` 等不存在的目标保持原样，点击可能在 iframe 内 404
  或跳出 DSH 路由；这是报告自身内容，插件不做兜底。
- 公开外链依赖 Caddy 的 `/kb-public` 路由与 `KB_PUBLIC_BASE_URL` 一致；换域名/换前缀时两处
  要同时改。公开标记是「按报告」的，没有密码/有效期；取消公开后旧链接立即 404。
