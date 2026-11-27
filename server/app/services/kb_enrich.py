"""P1：生成端 sidecar —— 给每篇报告抽取结构化元数据。

产出四类信息，写进索引库（SQLite）而不是报告文件本身，报告目录保持只读：

- `summary_ai`：80–140 字的中文摘要（比正文首个 <p> 更完整）；
- `key_points`：3–5 条要点；
- `entities`：人名/公司/产品/主题等实体（type + name）；
- `tags`：5–8 个可检索标签。

调用方式：
- 后台自动：main.py 的 lifespan 起一个低频线程，每次补一小批；
- 手动/外部（如 Hermes 写完报告后）：`python -m app.cli_enrich --limit 20`。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app import config
from app.services import kb_ai, kb_store

logger = logging.getLogger("dsh-kb.enrich")

_SYSTEM = (
    "你是个人知识库的索引器。阅读一篇报告后，只输出一个 JSON 对象，字段："
    '{"summary":"80-140字中文摘要，客观、信息密度高",'
    '"key_points":["3-5条要点，每条不超过40字"],'
    '"entities":[{"type":"公司|人物|指数|产品|主题|地点","name":"实体名"}],'
    '"tags":["5-8个可用于检索的短标签，2-6字"]}'
    "。不要输出 JSON 以外的任何文字；不要编造文中没有的信息。"
)

_MAX_TAGS = 8
_MAX_POINTS = 5
_MAX_ENTITIES = 12


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _parse_payload(raw: dict) -> dict:
    """把模型输出收敛成稳定结构：长度/数量/类型都做硬约束。"""
    summary = _clean_text(raw.get("summary"), 300)
    points = [
        _clean_text(item, 60)
        for item in (raw.get("key_points") or [])
        if str(item or "").strip()
    ][:_MAX_POINTS]
    tags = []
    for item in raw.get("tags") or []:
        tag = _clean_text(item, 12)
        if tag and tag not in tags:
            tags.append(tag)
    entities = []
    seen = set()
    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 40)
        kind = _clean_text(item.get("type"), 12) or "主题"
        if not name or (kind, name) in seen:
            continue
        seen.add((kind, name))
        entities.append({"type": kind, "name": name})
    return {
        "summary_ai": summary,
        "key_points": points,
        "entities": entities[:_MAX_ENTITIES],
        "tags": tags[:_MAX_TAGS],
    }


def _build_user_prompt(item: dict) -> str:
    body = (item.get("body") or item.get("summary") or "")[: config.AI_BODY_LIMIT]
    return (
        f"标题：{item.get('title') or ''}\n"
        f"分类：{item.get('category') or ''}\n"
        f"系列：{item.get('series') or ''}\n"
        f"日期：{item.get('date') or ''}\n\n"
        f"正文（截断）：\n{body}"
    )


def _checked(raw: dict) -> dict:
    payload = _parse_payload(raw)
    if not payload["summary_ai"]:
        raise kb_ai.KBAIError("模型未返回 summary")
    return payload


def enrich_one(item: dict) -> dict:
    """抽取单篇；成功返回清洗后的 payload，失败抛 KBAIError。

    模型偶尔会输出不合法 JSON（长正文 + 采样温度导致）。第一次失败后
    用「更短正文（2500 字）+ 温度 0 + 更严格的输出约束」再试一次，避免直接判死。
    """
    try:
        return _checked(kb_ai.chat_json(_SYSTEM, _build_user_prompt(item)))
    except kb_ai.KBAIError as exc:
        if "鉴权" in str(exc) or "未配置" in str(exc):
            raise
        logger.info("AI 首次抽取失败（%s），用短正文重试：%s", item.get("path"), exc)
        short = dict(item)
        short["body"] = (item.get("body") or "")[:2500]
        raw = kb_ai.chat_json(
            _SYSTEM + "\n严格要求：输出必须是单个合法 JSON 对象，不能有注释、省略号或多余文字。",
            _build_user_prompt(short),
            temperature=0.0,
        )
        return _checked(raw)


def enrich_report(path: str, *, force: bool = False) -> dict:
    """按路径抽取一篇（供手动触发）。"""
    item = kb_store.get_report(path)
    if item is None:
        return {"ok": False, "error": "报告不存在或尚未建立索引"}
    if item.get("ai_version", 0) >= kb_store.AI_VERSION and not force:
        return {"ok": True, "skipped": True, "path": path}
    # get_report 的 preview 只有 1200 字，这里直接用正文（body 不返回给前端）。
    item = dict(item)
    item["body"] = _full_body(path)
    payload = enrich_one(item)
    kb_store.set_ai(path, payload, model=kb_ai.model_label())
    return {"ok": True, "path": path, "summary_ai": payload["summary_ai"], "tags": payload["tags"]}


def _full_body(path: str) -> str:
    """优先用索引里的正文；文件型报告库内没有正文时才回落到磁盘。"""
    from app.services import kb_index

    conn = kb_store._connect()  # noqa: SLF001（同包内部工具）
    try:
        row = conn.execute("SELECT body FROM reports WHERE path = ?", (path,)).fetchone()
    finally:
        conn.close()
    if row and (row["body"] or "").strip():
        return row["body"]
    _, body = kb_store._extract_text(kb_index.REPORTS_DIR / path)  # noqa: SLF001（同包内部工具）
    return body


def enrich_pending(limit: int = 3, *, include_failed: bool = False) -> dict:
    """补一小批 pending 报告；返回统计。"""
    info = kb_ai.status()
    if not info.get("available"):
        return {"ok": False, "skipped": True, "reason": info.get("reason") or "模型不可用",
                "enriched": 0, "failed": 0, "remaining": 0}
    if not kb_ai.is_configured():
        return {"ok": False, "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）", "enriched": 0, "failed": 0, "remaining": 0}
    items = kb_store.next_pending(limit=limit, include_failed=include_failed)
    enriched = failed = 0
    errors: list[str] = []
    model = kb_ai.model_label()

    def run(item: dict) -> tuple[str, str]:
        """在线程里抽一篇；返回 (path, 错误信息或空串)。"""
        try:
            payload = enrich_one(item)
            kb_store.set_ai(item["path"], payload, model=model)
            return item["path"], ""
        except Exception as exc:  # 单篇失败不影响整批
            message = f"{type(exc).__name__}: {exc}"[:300]
            kb_store.set_ai_error(item["path"], message)
            return item["path"], message

    with ThreadPoolExecutor(max_workers=config.AI_CONCURRENCY) as pool:
        for path, message in pool.map(run, items):
            if message:
                failed += 1
                errors.append(f"{path}: {message}")
                logger.warning("AI 抽取失败 %s: %s", path, message)
            else:
                enriched += 1
    stats = kb_store.ai_stats()
    return {
        "ok": True,
        "requested": limit,
        "enriched": enriched,
        "failed": failed,
        "remaining": stats["pending"],
        "stats": stats,
        "errors": errors[:5],
    }
