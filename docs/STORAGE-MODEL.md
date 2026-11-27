# 知识库存储模型调研：从「日期文件夹」到「文档记录 + 视图」

> 2026-11-25 · 对象：dsh-kb（索引 523 篇 / 磁盘 525 个 HTML）· 结论先行，证据在后

## 0. 结论

你的直觉是对的，而且在你这个库里不是审美问题，是可以量出来的数据错误：

- **397/525 个 HTML 在同一个目录 `20260922/`**（其中 396 个在 `20260922/mc-versions/`）。它们的「日期」被系统算成 2026-09-22，而内容实际覆盖 2009–2026 的 Minecraft 版本记录。
- **83 个日期目录里，52 个只有 1 篇、72 个 ≤2 篇**。日期目录记录的是「哪天入库」，不是「哪天的东西」。
- 唯一的新增入口 `/kb/clip`，实现是「把网页抓下来写成 `YYYYMMDD/clip-*.html`，再调 `sync()`」。也就是说：**投喂 = 写文件，文件路径 = 身份**。

所以该做的不是「换一套更聪明的文件夹规则」，而是把现在被路径绑死的四件事拆开：**内容、记录、视图、连接器**。

两条硬约束先说清楚（逐文件盘点后确认）：

- **`date` / `category` / `series` 对现有 523 行全部由路径和文件名推导**，库里没有任何独立事实源。换身份必须和「写入者交元数据」同批上线，否则这三列会集体退化成空值。
- **`reports.id` 不能当对外身份**：它是旁挂自增列（只有 `report_tags` 用它），文件删掉再出现会拿到新 id。`content_hash` 虽然算了 sha1，但目前只当「图谱/Wiki 是否过期」的缓存指纹，没有唯一索引、没有一处按它查询，**现在也当不了去重键**。要先补一个稳定 `uid`，再谈换 URL。

## 1. 文件夹现在到底在承担什么

| 角色 | 位置 | 现状 |
|---|---|---|
| 身份 | `server/app/services/kb_store.py:40,613` | `path TEXT NOT NULL UNIQUE` 是事实身份；`id` 只在 `report_tags` 里用，删/重建同一 path 会换新 id |
| 日期 | `server/app/services/kb_index.py:94` | `_date_for(rel)` 只认 `^(\d{8})/` 目录名，**不从内容取** |
| 系列 | `server/app/services/kb_index.py:102,136` | 从文件名 token 推（mc-versions、个股、来源平台） |
| 扫描 | `server/app/services/kb_index.py:180` | `rglob('*.html')`，标题/分类/日期/系列全在扫描时推导 |
| 唯一写入者 | `server/app/services/kb_clip.py:365` | 抓正文 → 写 `YYYYMMDD/clip-*.html` → `sync(force=True)` |
| 读取 | `server/app/api/routes/reports.py:48` | `/reports/{path}` 直接映射文件系统 |
| 公开站 | `server/app/api/routes/public_site.py:169` | `/kb-public/reports/<rel>`，内链按 path 改写 |

`path` 还被当成外键串进其他表：`report_events.path`、`wiki_source.path`、`wiki_article_source.path`、`compile_job.report_path`。所以「改身份」会牵动图谱与 Wiki 的引用，不只是存储层。

也有好消息：
- 标签已经走 `report_tags.report_id`，不是 path；实体、图谱页、Wiki 页的键是 slug，也跟 path 无关；
- 语义字段（title / category / series / date / tags / summary / body）已经在库里，缺的是「谁提供」；
- host 半（`lib/index.js:21,186`）和 Caddy（`/etc/caddy/Caddyfile:89-95`）只认 `/kb` 前缀，换身份对这两层零改动；检索结果里已经有 `article:<slug>` / `node:<slug>` 这种非路径 id 的先例（`kb_store.py:1836,1887`），前端本来就能容忍；
- 外部写入者共 4 处：`market-report`（目录和文件名各写一次日期）、`/root/scripts/digest`（`mkdir -p $KB/$DATE`）、插件自己的剪藏、`fleet/scripts/import-*.py`（默认目录还指向已废弃的 `betterlife/reports`）。

## 2. 数据本身说明了什么

| 指标 | 值 |
|---|---|
| HTML | 525 个，合计 **8.7 MB**，平均 17 KB，最大 96 KB |
| 非 HTML（图片） | 326 个，≈120 MB，单张最大 6.5 MB |
| 未入索引 | 2 个（`index.html` 与 `shares.html` 在 `_SKIP_FILES` 里） |
| 死链 | **517/525** 仍在引用已下线的 `/mine/reports/assets/shell.css` |

三条判断：

1. **文本极小、图片是大头**。8.7 MB 全量塞进 SQLite 毫无压力；120 MB 图片才需要独立存放。
2. **日期目录 ≈ 入库批次**。用它当内容日期，等于把 76% 的库钉在同一个错误日期上。
3. **路径已经泄漏进公开链接**（`/kb-public/reports/<rel>`），换身份必须留别名。

> 2026-11-25 更新：这批 mc 历史已按要求清除（395 篇报告 + 213 个图谱页 + 351 篇 Wiki 文章），
> 归档在 `/root/backups/mc-versions-20261125.tar.gz`。本节数字保留，作为「日期目录 ≈ 入库批次」的证据。

## 3. 别人怎么做的

### 3.1 条目 + 附件（Zotero）

Zotero 的身份是「条目」（元数据），文件只是挂在条目下的附件，由应用自己管理：文件存在数据目录里，「每个文件有自己的子目录，目录名是随机的 8 位字符串」[3]。分类也不是文件夹——「把条目加进多个 collection 不会复制条目，collection 更像音乐播放列表而不是文件系统里的文件夹」，并配套「保存的搜索」当动态集合[2]。

### 3.2 对象 + 关系（Anytype）

Anytype 里一切都是对象，靠类型/属性/链接组织。它的文档把差别说得最直白：**「Folders ask 'where does this go?'… Objects ask 'what does it relate to?'」**，并明确「对象没有文件夹层级，最好的整理方式是用 Views」[1]。

### 3.3 单文件数据库（SQLite）

SQLite 数据库是单个磁盘文件、跨平台、格式向后兼容，官方把它推荐为「应用文件格式」，美国国会图书馆也把它列为长期保存的推荐存储格式[4]。这正好符合你「零依赖、单机、可整份拷走」的偏好。

**共同点**：成熟产品都把「字节放哪」和「怎么找」分开。文件系统只负责字节，不负责语义。

## 4. 建议的模型：四层分离

```
连接器            文件系统扫描（Hermes / market-report 现状，保留）
                 剪藏 URL / 上传 / 手机分享 / 飞书消息（新增）
                    ↓  规范化 + content_hash 去重
文档记录          SQLite：id / title / published_at / source / series /
                 category / tags / html / content_hash / read / starred / public
                    ↓
原始字节          HTML 文本 → SQLite TEXT 列（8.7MB 全量可存）
                 图片 / PDF → data/blobs/<sha[0:2]>/<sha>.<ext>（内容寻址）
                    ↓
视图              时间线 / 系列 / 分类 / 标签 / 保存视图 / 语义检索
```

关键点：

- **先加稳定 `uid`**（入库时生成 ULID；存量一次性回填 path→uid 映射），`path` 降级为 `storage_key`，只作定位与兼容别名。
- **`published_at` 独立于入库时间**。有就填（文件名里的 8 位日期、HTML 里的发布时间），没有就留空并标 `unknown`，绝不再用目录名兜底。
- **`content_hash` 现在是缓存指纹，不是去重键**。`kb_graph.py:353`、`kb_wiki.py:193` 用它拼 `path:hash` 判「节点/Wiki 是否过期」；要它当去重键，得加唯一索引并把写入改成「先查 hash 再插入」。
- **HTML 进库**：8.7 MB、最大 96 KB，放 TEXT 列即可；渲染时把 `/mine/...`、`reports/...` 改写成站内路径（`public_site.py` 已有内链改写的前例）。
- **图片走内容寻址**：不进日期目录，按 sha 分片，天然去重、改名不断链。注意公开站的附件白名单现在是**扫报告同目录**得出的（`kb_public.py:169-199`），换存储后要改成「uid → 资产清单」。
- **写入者契约与 uid 同批上线**：`published_at` / `source` / `series` 由写入者或入库 API 显式提供，`kb_index.scan()` 从「身份来源」降级成「导入适配器」。

建议的 schema 增量（`reports` 表原地扩展，不动现有数据）：

```sql
-- 1) 稳定身份：对外只认 uid（不要复用 report_tags 已经在用的 id）
ALTER TABLE reports ADD COLUMN uid TEXT NOT NULL DEFAULT '';           -- 入库时生成 ULID
CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_uid ON reports(uid);

-- 2) 元数据事实源（不再从路径推导）
ALTER TABLE reports ADD COLUMN published_at TEXT NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy-fs';
ALTER TABLE reports ADD COLUMN storage_key TEXT NOT NULL DEFAULT '';   -- 原 path，仅兼容/定位
ALTER TABLE reports ADD COLUMN html TEXT NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN blob_sha TEXT NOT NULL DEFAULT '';

-- 3) 让 content_hash 真的能去重（现在没有唯一约束）
CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_hash
  ON reports(content_hash) WHERE content_hash != '';
```

## 5. 迁移路线（每步可单独验收）

| 阶段 | 内容 | 估时 | 验收 |
|---|---|---|---|
| **P0** | `published_at` 独立成字段并回填（文件名/标题里的日期；Minecraft 版本标 unknown） | 0.5d | `20260922/mc-versions` 的 396 篇不再全部显示 2026-09-22；日期筛选仍可用 |
| **P1** | 加 `uid` + `UNIQUE(content_hash)`；`POST /kb/ingest`（text / html / url）只写记录不写文件；`/kb/clip` 改走它；新增 `/kb/doc/<uid>`，旧路径 301（API 与前端 URL 分两步切） | 2d | 剪藏一篇后 `reports/` 不新增文件，列表能出现、能打开 |
| **P2** | 左导航换视图（时间线 / 系列 / 标签 / 保存视图），不再暴露文件夹 | 1.5d | 新条目出现在「本周」，用户不需要知道它存在哪 |
| **P3** | 存量 525 篇 HTML 入 DB、图片进 blob、`reports/` 打 tar 归档；517 处 `/mine/` 死链改写 | 1d | 把 `reports/` 改个名，全站仍可读 |
| **P4**（可选） | 多入口投喂：手机分享（DSH `phone_shares`）、Hermes 飞书、RSS | 1–2d | 手机分享一条链接，30 秒内出现在知识库 |

**做完 P0 + P1 就能解决你现在的烦**：以后投喂是「丢内容给知识库」，不用先想存哪、放哪个日期目录。

改动的真实体量（逐文件盘点后的数）：**6 处接口形状**（`/api/kb/report`、`state`、`bulk`、`events`、AI 摘要/分类、`/reports/{path}`）、**3 张 path 派生表**（`report_events`、`wiki_source`、`wiki_article_source`，外加 `compile_job.report_path`）、**4 个写入者**。host 半与 Caddy 零改动。

## 6. 不做什么

- **不换 Obsidian / Notion / Anytype**：Obsidian 的库就是文件夹；Notion/Anytype 不能在这台 2C/8G 的机器上 headless 跑，还会丢掉现有语义检索、图谱、Wiki 与管线集成。借鉴它们的**模型**，不搬产品。
- **不引对象存储 / 向量数据库**：8.7 MB 文本 + 120 MB 图片，SQLite + numpy 足够。
- **不把日期文件夹换成月份/年份文件夹**：只要身份还是路径，问题会以另一种形式回来。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 可调试性下降（以前 `ls` 就能看） | `GET /kb/doc/<id>/html` + `kb export --legacy-tree`（按旧布局导出，随时回到文件视角） |
| 备份变复杂 | 两件东西：`kb.sqlite` + `data/blobs/`；文本进库后备份体积反而更可控 |
| 公开链接失效（已发 4 条） | 保留 `origin_path` 别名，`/kb-public/reports/<path>` 继续可用 |
| 管线兼容 | Hermes / market-report 先继续写文件（文件系统连接器照旧扫），P4 再改成 POST |
| 大 HTML / 图片撑爆 DB | 图片一律不进 DB；HTML 单篇 >1 MB 时回落成 blob |

## 8. 顺带发现的 5 个现存问题（跟存储模型无关，迁移时值得一并修）

1. **标签会留孤儿行**：没有开 `PRAGMA foreign_keys`，删报告不清 `report_tags`（`kb_store.py:369-373`），只靠 `tags.reports` 计数兜底；删掉再重建同一路径会让标签计数虚高。
2. **AI 摘要二次读文件**：`kb_enrich.py:131-135` 直接读 `REPORTS_DIR / path`，不复用库里的 body；文件不在就补不了摘要。
3. **公开附件白名单靠扫目录**：`kb_public.py:169-199` 按报告同目录找 `images/<stem>/` 和同级文件，换存储必须改成「uid → 资产清单」。
4. **周报的分类值不在白名单**：`digest.mjs:218` 写 `<!-- bl:category: 周度推送 -->`，而 `kb_index.py:28-41` 的枚举里没有这个值；HTML 注释覆盖不做校验，入库原样生效。
5. **fleet 的导入脚本还指向已废弃目录**：`import-blog-to-kb.py:119`、`import-frankbase-article.py:28` 默认 `betterlife/reports`，跑起来会写进死路径。

## 9. 需要你定的三件事

1. **图片要不要进索引**？现在只索引 HTML，图片靠 HTML 里的相对路径引用，搜索结果里搜不到图。
2. **第一个投喂入口做哪个**：URL 剪贴 / 手机分享 / 飞书机器人？
3. **存量怎么处理**：入库后直接删 `reports/`，还是先归档 tar 跑一周再删？（我倾向先归档。）


## 10. 实现状态（2026-11-25）

P0 / P1 / P2 / P3 与 Office 预览已落地：

| 能力 | 实现 | 验证 |
|---|---|---|
| **P0 发布时间** | `published_at` 独立字段，取值顺序：文件名 8 位日期 > 标题日期 > 目录名（`kb_index._published_for`）；`app.cli_backfill.py` 回填存量 | 128 篇全部回填，`date == published_at[:10]`；非法目录日期不再产生脏值 |
| **P1 投喂入口** | `POST /api/kb/upload`（多文件）/ `POST /api/kb/ingest`（文本 / 链接）+ 面板「投喂」 | 浏览器实测：上传 Markdown、粘贴文本均入库并出现在列表 |
| **P1 多格式抽取** | `kb_extract.py`：html / md / txt / pdf / docx / xlsx / pptx / 图片 / 音视频 | PDF 3619 字 + 首页缩略图；PNG 缩略图 |
| **P1 音视频转写** | 本地 SenseVoice（sherpa-onnx），ffmpeg 抽音轨；无音轨自动跳过 | 18.45s 视频转写命中原文；搜索「交接」能搜到音频/视频文档的转写句 |
| **P1 私有 blob** | sha256 内容寻址 + AES-256-GCM（AAD 绑定 sha），密钥 `/root/.secrets/kb-blob.key` | 12MB 随机文件主存 COS，按 sha 取回逐字节一致 |
| **P1 COS 策略** | >10MB 主存 COS（本地不留原件）；小文件本地 + 后台加密镜像 | 实测 `kb-private/blobs/...enc` 全部可解密；删除后 COS 对象同步清掉 |
| **P2 左侧导航** | 时间线（今天/本周/本月/今年/更早 + 月份）、系列、标签、保存视图；分类折叠到底部 | 时间线「本月」=39、系列「A股市场全景」=20，保存视图可建可应用 |
| **P3 死链修复** | 121 个报告里的 356 处 `/mine/...` 改成随深度计算的相对路径；`assets/shell.{css,js}` 与 `/kb/static/icon-192.png` 实测 200 | 迁移后 blob 内是 `/kb/reports/assets/...` 绝对路径，沙箱 iframe 正常渲染 |
| **P3 存量迁移** | `app/cli_migrate_reports.py`：**全部**文件型 HTML → blob + doc 行（含公开的 4 篇；公开报告保持原文与相对素材，继续按原 URL 由 `/kb-public/reports/<path>` 从 blob 输出）；私有报告 HTML 改由 `doc/<uid>` 沙箱 iframe 渲染 | 4 个公开 URL 字节级一致、配图 200；`reports/` 里 HTML 归零（只剩 assets/images）；公开 doc 的查看页嵌 `/kb-public` 链接 |
| **P3 附件迁移** | `app/cli_migrate_assets.py`：`reports/` 下 326 个非 HTML 附件（图片/CSS/JS/数据文件，125MB）→ `kb_blob` + `asset(path→sha)` 表，路由按原路径解密输出；迁完删除原文件并清空 99 个空目录 | `/kb/reports/*` 与 `/kb-public/reports/*` 均 200；抽样 3 个文件与备份 sha256 一致；公开页 7 张图 0 失败 |
| **Office 预览** | LibreOffice 24.8.6.2（dnf 安装）；docx/xlsx/pptx → PDF → 首页 PNG 缩略图 | 三个格式实测产出 PNG（2–8s），中文渲染正常；无 soffice 时优雅降级 |
| **写入端接入** | market-report（每日 15:10，Hermes cron）、digest（每周六 09:01，systemd timer）与剪藏都调 `/root/scripts/kb/kb_publish.py` / `kb_clip._persist` 投喂；投喂失败直接报错退出，不再回落写文件 | 真实 build 产物投喂成功；重复投喂幂等；投喂脚本缺失时 exit 1 |

测试基线：**125 passed**（含新增 `tests/test_ingest.py` 16 条、`tests/test_extract.py` 37 条）。

> **迁移注意（2026-11-25 实测踩坑）**：`sync()` 的 upsert 补了
> `WHERE COALESCE(reports.doc_kind,'file')='file'` 守卫。没有它时，迁移后服务里
> 残留的扫描缓存会把 DB 文档行当成「文件内容变化」，走 `ON CONFLICT` 重置 AI 字段
> （`ai_version` 归零、`entities` 清空）。本次发生过一次，已从迁移前备份恢复 74 篇
> 的 AI 字段与标签关系，并重建图谱（119 页 / 992 条来源）。


## Sources

[1] https://doc.anytype.io/anytype/create/objects.md
[2] https://www.zotero.org/support/collections_and_tags
[3] https://www.zotero.org/support/attaching_files
[4] https://www.sqlite.org/onefile.html
