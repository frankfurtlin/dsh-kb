"""R1：AI 分类 —— 让模型给每篇报告选分类与二级系列。

与 P1（`kb_enrich`）分开的原因：分类不需要全文，用「标题 + 日期 + 规则分类 + 标签 +
已有摘要」就能判断，单次调用更便宜；重跑分类不牵动摘要/标签，反之亦然。

提示词约束：
- 分类必须落在 `kb_index.CATEGORIES` 枚举内，模型越界时回落到规则值并记录原因；
- 系列用于把同主题报告聚在一起（如「1.x 正式版」「A股市场全景」「名称(代码)」），
  允许为空（表示不值得单独成组）。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app import config
from app.services import kb_ai, kb_index, kb_store

logger = logging.getLogger("dsh-kb.classify")

CATEGORY_HINTS = {
    "Minecraft 版本": "按版本阶段给系列：Classic 0.x / 远古版本 · Infdev / 远古版本 · Indev / Alpha 阶段 / Beta 阶段 / 1.x 正式版 / 26.x 新版 / 快照 YY 年",
    "市场全景": "系列用「A股市场全景」",
    "个股分析": "系列用「名称(代码)」，如「中国铝业(601600)」；持仓类用「我的持仓」",
    "话题精读": "按来源给系列：小红书爆款笔记 / 公众号长文 / YC 海外文章 / AIRP 综述 / 其他精读",
    "策略研究": "系列用「量化策略」",
    "旅行影像": "系列用「旅行影像册」",
    "影像收录": "系列可留空或按主题归并",
    "技术演示": "系列可留空或按主题归并",
    "其他": "系列尽量留空；确实同类较多时再起一个 2-12 字的组名",
}

_SYSTEM = (
    "你是个人知识库的分类器。根据报告标题、日期、已有规则分类、标签和摘要，"
    "从下列分类中选出最贴切的一个，并给一个二级系列名。只输出 JSON："
    '{"category": "分类名（必须是枚举之一）", "series": "2-12 字的二级系列，可空", '
    '"reason": "不超过 30 字的判断依据"}。'
    "优先级：内容主题 > 标题关键词；宁可归入上级分类，也不要硬塞进不相关的分类；"
    "不要编造摘要里没有的信息。分类枚举：" + "、".join(kb_index.CATEGORIES)
)


# 系列名归一化：模型偶尔会把前缀/英文写法混进来（「远古版本 · Classic 0.x」），
# 同一系列分裂成多个左导航分组会让导航变吵，这里按关键词收敛到规则里已有的名字。
_SERIES_CANON = (
    ("classic", "Classic 0.x"),
    ("survival test", "远古版本 · Survival Test"),
    ("infdev", "远古版本 · Infdev"),
    ("indev", "远古版本 · Indev"),
    ("alpha", "Alpha 阶段"),
    ("beta", "Beta 阶段"),
)


def canonical_series(series: str) -> str:
    text = " ".join(str(series or "").split())[:40]
    lowered = text.lower()
    for keyword, name in _SERIES_CANON:
        if keyword in lowered:
            return name
    return text


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _rule_values(item: dict) -> tuple[str, str]:
    category = _clean(item.get("category_rule") or item.get("category"), 40)
    series = _clean(item.get("series_rule") or item.get("series"), 40)
    if category not in kb_index.CATEGORIES:
        category = item.get("category") if item.get("category") in kb_index.CATEGORIES else "其他"
    return category or "其他", series


def _build_user_prompt(item: dict) -> str:
    rule_category, rule_series = _rule_values(item)
    tags = item.get("tags") or ""
    if isinstance(tags, str):
        try:
            import json

            tags = "、".join(json.loads(tags or "[]")[:8])
        except (ValueError, TypeError):
            tags = ""
    summary = _clean(item.get("summary_ai") or item.get("summary"), 400)
    hints = CATEGORY_HINTS.get(rule_category, "")
    return (
        f"标题：{item.get('title') or ''}\n"
        f"日期：{item.get('date') or ''}\n"
        f"规则分类：{rule_category}（系列：{rule_series or '无'}）\n"
        f"标签：{tags}\n"
        f"摘要：{summary or '（无）'}\n"
        + (f"该分类的系列约定：{hints}\n" if hints else "")
    )


def _parse(raw: dict, item: dict) -> dict:
    """收敛模型输出；分类越界回落到规则值，系列为空时按同分类继承规则系列。"""
    rule_category, rule_series = _rule_values(item)
    category = _clean(raw.get("category"), 40)
    series = _clean(raw.get("series"), 40)
    reason = _clean(raw.get("reason"), 60)
    fallback = False
    if category not in kb_index.CATEGORIES:
        fallback = True
        reason = (reason + f"（模型分类越界：{category or '空'}，回落规则值）")[:60]
        category = rule_category
    if not series:
        series = rule_series if category == rule_category else ""
    series = canonical_series(series)
    return {
        "category": category,
        "series": series,
        "reason": reason,
        "fallback": fallback,
    }


def classify_one(item: dict) -> dict:
    """给一篇报告分类；失败抛 kb_ai.KBAIError。"""
    try:
        raw = kb_ai.chat_json(_SYSTEM, _build_user_prompt(item), max_tokens=600)
    except kb_ai.KBAIError as exc:
        if "鉴权" in str(exc) or "未配置" in str(exc):
            raise
        logger.info("AI 分类首次失败（%s），温度 0 重试：%s", item.get("path"), exc)
        raw = kb_ai.chat_json(
            _SYSTEM + "\n严格要求：只输出一个合法 JSON 对象。",
            _build_user_prompt(item),
            temperature=0.0,
            max_tokens=600,
        )
    return _parse(raw, item)


def classify_report(path: str, *, force: bool = False) -> dict:
    item = kb_store.get_report(path)
    if item is None:
        return {"ok": False, "error": "报告不存在或尚未建立索引"}
    if item.get("classify_version", 0) >= kb_store.CLASSIFY_VERSION and not force:
        return {"ok": True, "skipped": True, "path": path}
    payload = classify_one(item)
    result = kb_store.set_classify(path, payload, model=kb_ai.model_label())
    return {"ok": True, "path": path, **payload, "applied": result}


def classify_pending(limit: int = 5, *, include_failed: bool = False) -> dict:
    """补一小批待分类报告；返回统计。"""
    if not kb_ai.is_configured():
        return {
            "ok": False,
            "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）",
            "classified": 0,
            "failed": 0,
            "remaining": 0,
        }
    items = kb_store.next_pending_classify(limit=limit, include_failed=include_failed)
    classified = failed = 0
    model = kb_ai.model_label()

    def run(item: dict) -> str:
        try:
            payload = classify_one(item)
            kb_store.set_classify(item["path"], payload, model=model)
            return ""
        except Exception as exc:  # 单篇失败不影响整批
            message = f"{type(exc).__name__}: {exc}"[:300]
            kb_store.set_classify_error(item["path"], message)
            return message

    with ThreadPoolExecutor(max_workers=max(1, config.CLASSIFY_CONCURRENCY)) as pool:
        for message in pool.map(run, items):
            if message:
                failed += 1
            else:
                classified += 1
    return {
        "ok": True,
        "requested": len(items),
        "classified": classified,
        "failed": failed,
        "remaining": kb_store.classify_stats()["pending"],
        "stats": kb_store.classify_stats(),
    }
