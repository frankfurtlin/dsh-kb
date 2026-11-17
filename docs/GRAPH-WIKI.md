# 知识图谱 + LLM Wiki:调研、对比与可采纳清单

状态:方案稿(2026-11-17)。调研已完成,下一步按 §6「先做」四项实施。
依据:仓库实测数据(kb.sqlite 生产索引)+ 11 个开源项目的 README/仓库事实(由调研子任务用 GitHub API + raw README 核实,时点 2026-11-17)。

---

## 0. 一句话结论

**我们的缺口不是"再引入一个图谱/RAG 引擎",而是"一张 wiki 页表 + 一张反向引用表 + 一次增量化编译"。**
图与 wiki 都作为现有 SQLite 索引的**派生层**:实体/关系存关系表,wiki 存 markdown 文件 + 页表,检索继续用现有 0.62 语义 + 0.38 词法,新增的只是「图谱扩展」和「wiki 页」两路召回。不引入 Neo4j/Postgres/常驻图库,不引入 GraphRAG 社区检测,不把 AGPL 代码并入仓库。

---

## 1. 目标与边界

用户诉求(2026-11-17):

> 将我的知识库结合 知识图谱 以及 llm wiki。先去 github 上找找开源的好的实现,
> 先给我一些项目的对比优缺点,结合一下他们的以及我的插件的实际情况,给我一份可采纳的清单。

拆成四件事:

1. **实体/关系图谱**:报告里的实体(人物/公司/指数/版本/主题/方块…)连成图,能按实体导航、看共现关系、做图谱扩展检索。
2. **LLM Wiki**:agent 维护的一层 markdown 百科(实体页/主题页/索引/日志),人可以像 Obsidian 一样浏览,AI 负责维护。
3. **检索更准**:图谱和 wiki 都能反哺 `kb_ask`/`kb_search`,而不是两个孤岛。
4. **单机可跑、增量、便宜**:不引入图数据库/额外容器/GPU,不重跑全量 LLM。

**不做**:wiki 不当第二事实源;不引入需常驻的图数据库;不做多人协同编辑。

---

## 2. 现状基线(实测,2026-11-17 生产索引)

| 指标 | 值 |
| --- | --- |
| 报告数 | 473(`reports/*.html`,中文为主) |
| 索引库 | `server/data/kb.sqlite` 约 5.8 MB |
| AI 摘要覆盖 | 473/473;新增一篇成本约 3–4 s(DSH 桥,单并发) |
| 实体总量 | 2,600+ 条实体提及,去重后 **1,339 个实体名** |
| 实体类型 | 主题 781 / 产品 724 / 版本 275 / 指数 268 / 公司 159 / 人物 129 / 物品 74 / 方块 73 / 地点 61 / 生物 41 / 行业 34 / 功能 17 |
| 高频实体 | ≥8 篇的只有 30 个;前 5:Minecraft Java版 267、中文Minecraft Wiki 72、Alpha v1.0 71、Indev 0.31 60、Mojang 54 |
| 标签 | 924 个 / 3,394 条报告-标签关系 |
| 向量 | 473/473,512 维,L2 归一化,fastembed `bge-small-zh-v1.5`(本地 ONNX,无 GPU) |
| 检索 | 混合 0.62 余弦 + 0.38 词法;`kb_ask` top6 → RAG;`kb_search`/`kb_get`/`kb_ask` 已作为 DSH 工具暴露 |
| 变更轨迹 | `report_events` 80 条(ingest/reclassify/ai_enrich/state/public/classify) |

> 两个关键观察:
> ① **实体已经在库里**——AI 摘要阶段已产出 `entities[{type,name}]`,图谱第一步是「关系化 + 去重 + 补边」,不需要从头抽;
> ② **头号问题不是抽不全,是实体对齐**:1,339 个名字里 1,300+ 是长尾,同时存在「中文Minecraft Wiki / 中文 Minecraft Wiki」这类别名分裂;另外有误抽(如「如此」被当人物)。
> ③ **规模卡在 wiki 导航上**:Karpathy 原文说 `index.md` 目录导航「~100 源、几百页」还行,470 篇已经超出——wiki 页必须同时进向量检索,不能只靠目录。

---

## 3. 外部项目对比(已核实)

| 项目 | star / license / 近期 | 索引方式与成本 | 增量 / 删除 | 存储依赖 | 中文 | 查询方式 | 上手成本 | 对我们的适配判断 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| microsoft/graphrag | 36.7k / MIT / 2026-11-16 提交,README 自述「largely in maintenance mode, won't accept new PRs」 | 每 chunk LLM 抽实体关系 + Leiden 社区检测 + 每社区 LLM 报告;`standard` 全量、`fast` 省一点 | `standard-update/fast-update` 支持更新,但**删除非一等操作**(issue #1613 无正式修复) | parquet(文件/内存/blob/cosmosdb),不强制外部库 | 2.x 已无 `language` 配置,提示词全英文,中文要自己改 prompt | local / global / drift / basic | 高(5) | **不建议引入**。成本高、维护模式、删除弱;它的「全局主题摘要」对单票单日事实查询收益低 |
| HKUDS/LightRAG | 40.7k / MIT / 今天仍在提交 | 每 chunk 抽 LLM(建议非思考型模型);图+向量双层 | **增量插入 + 选择性删除**,靠索引期 LLM cache 重建受影响实体/关系,有 `full_entities/full_relations` 锚点 | 默认内存+文件(README 明说只适合小规模);生产推荐 PG 或 Neo4j/Milvus | `SUMMARY_LANGUAGE=Chinese` + 中文 README | local / global / hybrid / naive / **mix(默认)** | 中高(4) | **架构抄它,不引入**。增量与删除锚点最值得抄;它的双层检索思路可直接映射到我们的 0.62/0.38 |
| gusye1234/nano-graphrag | 4.0k / MIT | ~1100 行极简实现;README 明确「每次 insert 都重算社区并重新生成社区报告」 | **增量不可用**(插入即全量重算) | NetworkX + FAISS,零外部服务 | 无中文 prompt | naive / local / global | 低(2) | **只当教学参考**。470 篇分批导入会被它的社区重算拖死 |
| topoteretes/cognee | 33.4k / Apache-2.0 | 记忆平台化(remember/recall/improve/forget);可零 LLM key(本地 GLiNER + 本地 embedding) | 支持更新 | 全嵌入式:SQLite + LanceDB + Ladybug(Kuzu)本地图;单 Postgres 仅 demo | 支持中文文本 | 语义 + 图遍历 | 中高(4) | **可选参考**。「零外部依赖 + 本地抽取」思路好,但它的模型层与我们 DSH 桥重复;不引入 |
| getzep/graphiti | 32.9k / Apache-2.0 | 时序上下文图(双时态:旧事实失效而非删除);每 episode 多轮 LLM | 增量好(时间维度),但每 episode 多轮 LLM,作者自己提示 429 | **强依赖 Neo4j 5.26 / FalkorDB / Neptune** | 英文为主 | hybrid(向量+BM25+图遍历) | 高(5) | **不引入**(常驻图库太重);抄它的「边带 first_seen/last_seen + 失效而非删除」 |
| Karpathy LLM Wiki(gist) | 11,985 字节原文 | 无索引器:LLM 增量维护 markdown 三层(raw 不可变 / wiki 由 LLM 独占写 / schema 约定) | 天然增量(按源 ingest);lint 查矛盾/过期/孤儿 | 纯文件 + git + Obsidian | 中文内容没问题 | index.md 导航 + 全文;规模「~100 源、几百页」,更大推荐 qmd | 低(1) | **抄它的组织方式**:三层 + index/log + ingest/query/lint 三种操作 |
| gd4ai/obsidian-llm-wiki | 797 / Apache-2.0 | Karpathy 模式的 Obsidian 插件;**零 embedding、零向量库** | ingest/update/lint + Smart Fix | Obsidian vault | 14 种输出语言含简体中文;15+ provider 含 OpenAI 兼容 | 在 `[[wiki-link]]` 图上跑 **Personalized PageRank + Monte Carlo PPR** | 低(2) | **抄它的「链接图 PPR」**:wiki 页 ≥150 时作为第三路召回,替代不了向量但互补 |
| atomicstrata/llm-wiki-compiler | 2.3k / MIT / TS+Node24 | 两阶段 LLM:抽概念 → 生成 typed pages(concept/entity/comparison/overview);复核门 + 新鲜度 badge | 支持再编译 | README 未写明存储引擎(未核实) | 英文为主 | 语义 chunk + BM25 rerank + wikilink 扩展 | 中(3) | **参考页面契约**(typed pages、profile 生命周期),不引入(TS 服务与现有 Python 栈重复) |
| gavischneider/awesome-llm-wiki | 281 / NOASSERTION | 清单,不是工具 | — | — | — | — | — | 只当信息源 |
| basicmachines-co/basic-memory | 4.6k / **AGPL-3.0** | **markdown 文件=唯一真源,SQLite(默认)/Postgres 只是索引**;FastEmbed 本地 embedding + hybrid 全文/向量 + 可选 rerank;无 per-chunk LLM | 改文件即改真源,索引增量 | SQLite 足够,无服务 | 支持(全文/向量中文可用) | MCP 工具:search/list/context/read/write;`[[links]]` 图 | 低(2) | **最像我们该长成的样子**:文件真源 + SQLite 索引 + 本地 FastEmbed hybrid + MCP 工具。AGPL 只抄设计不抄代码 |
| khoj-ai/khoj | 38.2k / **AGPL-3.0** / 最近提交 2026-08-02(3 个多月无提交) | 自托管个人 AI(docs/PDF/notes 语义检索 + agents) | 增量索引 | docker-compose 五容器:pgvector + searxng + terrarium + computer + server | 支持中文 | 语义 + agent | 高(5) | **不引入**(五容器);它的「个人 AI 入口」定位与我们 DSH 面板重复 |

**补充判断**:graphrag/graphiti/khoj 是「重量级正确解」,但对我们 473 篇、单机 2C4G、无 GPU 的约束都过重;nano-graphrag 增量不可用;cognee/llm-wiki-compiler 与现有 Python/DSH 栈重复。真正值得抄的是 **LightRAG 的增量与删除锚点** + **Karpathy/basic-memory 的「文件真源 + SQLite 索引 + 本地 hybrid」** + **obsidian-llm-wiki 的 PPR 召回**。

---

## 4. 目标架构(三层,全部本地)

```
reports/*.html                    ← 第 1 层:原始源(不可变,已有)
     │ 索引 sync(已有)
     ▼
kb.sqlite                         ← 第 2 层:索引 + 图谱 + wiki 页表(扩展)
  ├ reports / report_tags / tags / report_events        (已有)
  ├ entities(id,name,type,aliases,...)                  (新增)
  ├ report_entities(report_id,entity_id,count)          (新增)
  ├ entity_edges(src,dst,predicate,report_id,confidence)(后做)
  ├ wiki_page(id,slug,kind,title,body_md,source_hash,state)(新增)
  ├ wiki_link(page_id,target_slug)                      (新增)
  └ wiki_source(page_id,report_id,contribution)         (新增,删除/失效唯一抓手)
     │ 编译(后台 worker + DSH 工具)
     ▼
wiki/                             ← 第 3 层:agent 维护的 markdown(可 git、可 Obsidian)
  ├ SCHEMA.md / index.md / log.md
  ├ entities/<slug>.md
  ├ topics/<slug>.md
  └ queries/<date>-<slug>.md
```

原则:

- **reports/ 永远是唯一事实源**;图谱与 wiki 都是派生物,可用 `--rebuild` 从索引重建。
- **图谱写库是确定性的**(从已有 entities/tags 生成,不调 LLM);只有「别名合并判断」和「wiki 正文」调 LLM,且带 hash 缓存、可中断。
- **删除/更新靠反向引用**:报告消失或 hash 变化时,用 `wiki_source` 找到受影响页面,标 `stale` 而不是删(对标 LightRAG 的 full_entities 锚点)。
- **wiki 正文与 raw chunk 一起进向量表**(`owner_type='raw'|'wiki'`),因为 470 篇已超目录导航上限。

---

## 5. 分阶段落地清单

### 先做(1–2 周,四项都可独立验收,零新依赖)

1. **wiki 页表 + 反向引用(迁移脚本,零 LLM)**
   - `wiki_page` / `wiki_link` / `wiki_source` 三张表;`GET /api/wiki/page/{slug}` 只读;`wiki_page.source_hash` 记输入报告 hash。
   - 为什么:这是整个 wiki 层的最小骨架;`wiki_source` 是之后删除/失效的唯一抓手。
2. **`reports.content_hash` + `compile_job` 增量编译**
   - `compile_job(report_id,page_id,status,attempts)`;hash 命中即跳过;后台 worker 与现有 `_enrich_worker` 同构。
   - 为什么:守住 3–4s/篇成本线,避免"改一行重跑全量"。
3. **向量表扩 `owner_type('raw'|'wiki') + owner_id`**
   - wiki 页正文入向量;`kb_ask` 的 top6 候选合并 raw 与 wiki,权重先保持 0.62/0.38 不变,wiki 命中在 citations 标「wiki」。
   - 为什么:470 篇靠 index.md 导航不够,wiki 必须可检索;这一步让 wiki 立刻产生价值。
4. **只读 Lint 接口**
   - `GET /api/wiki/lint`:孤儿页 / 断链 / source_hash 过期 / 缺摘要 / 高频无页实体(如 ≥8 篇还无页的实体)。
   - 为什么:没有 lint,wiki 三个月后必然腐化;先只读,后续再自动修。

### 后做(2–4 周,体验增益)

5. **实体边表 `entity_edges(src,dst,predicate,report_id,confidence)`**
   - 单行保留来源报告(不聚合成一行);主题页用 `GROUP BY` 生成视图;**不跑 Leiden 社区检测**。
   - 为什么:GraphRAG 的社区报告全量重算不可接受,SQL 视图足够回答"这个主题下有哪些报告/实体"。
6. **`wiki_link` 图上的 PPR 作为第三路召回**(权重 0.15 起步,等 wiki 页 ≥150 再上)
   - 抄 obsidian-llm-wiki:零 embedding 的链接图检索,与向量/词法互补。
7. **wiki 生命周期**:`state='stale'|'archived'` + `wiki_page_rev` 版本表;lint 报告驱动 agent 重编译。
8. **品质门禁**:50 个实体人工判定别名合并/共现准确率;`kb_ask` 20 题评测集(Recall@5、引用正确率)。

### 不做(至少现在不做)

9. **不引入 GraphRAG 社区检测 + 社区摘要**:成本高、中文 prompt 要自己维护、删除弱;改用「高频实体 + SQL 主题视图」。
10. **不引入 Neo4j/Postgres/Graphiti 作为运行时**:1,339 实体不配;需要时再迁。
11. **不把 AGPL 代码(basic-memory/khoj)并入仓库**:只抄设计与数据格式。
12. **不做每 chunk 全量 LLM 重建 / 不做多人协同编辑**。

---

## 6. 建议动手顺序

```
建表(wiki_page/link/source + content_hash + compile_job)
  → content_hash 增量编译 worker
  → wiki 正文入向量 + kb_ask 候选合并
  → lint 只读接口
  → (等页面数 ≥150)实体边 + PPR
```

四步各自可独立验证:表结构可单测;增量可用"改一篇报告只编译它"验证;检索可看 citations 里是否出现 wiki;lint 可对现有 473 篇跑一遍看报告质量。

---

## 7. 验收指标(建议)

- 图谱:`report_entities` 覆盖 473/473;实体名去重后 ≤1,100(别名合并后);高频实体(≥8 篇)30 个全部有页。
- 检索:20 题评测集 Recall@5 比当前基线 +10%(图谱/wiki 召回升效时);`kb_ask` 引用 100% 落在真实报告/wiki 页。
- wiki:≥30 实体页 + ≥5 主题页;每页 ≥2 来源、≥2 `[[wikilink]]`;`log.md` 每次 ingest 有记录;lint 孤儿页=0。
- 成本:新增 LLM 调用只在「别名确认」与「wiki 正文」,且 hash 命中即跳过;单篇重编译 ≤1 次 LLM 调用。

---

## 8. 风险

- **AI 成本失控**:别名确认与 wiki 正文是新增调用;必须 hash 缓存 + 小批量 + 可中断,别名只对候选对调用。
- **实体质量**:现有 entities 来自摘要提示词,有误抽(「如此」当人物)与别名分裂;上线前先做停用词/长度/类型白名单清洗,并提供人工删除入口。
- **wiki 漂移**:页面必须带 `source_hash` + `generated_by/model/at`;lint 定期查孤儿/过期;报告变化时标 stale 而不是静默保留。
- **两种召回冲突**:wiki 页可能与 raw 报告内容重复;citations 里必须标明来源类型,权重先小步试(0.15)再调。
