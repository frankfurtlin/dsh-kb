"""P3：RAG 问答 —— 语义检索取证据，让模型只依据这些报告回答并标注出处。

流程：
1. 用混合检索（语义 + 词法）取 top 报告；
2. 每篇取 AI 摘要 + 正文前 1200 字拼成上下文；
3. 让模型输出 `{"answer": "..."}`，答案里带 [n] 引用；
4. 返回 answer + citations（n/标题/路径/日期/分类）+ related（其余相关报告）。

模型未配置或调用失败时返回结构化错误，前端提示，不影响检索功能。
"""

from __future__ import annotations

import logging

from app import config
from app.services import kb_ai, kb_search, kb_store

logger = logging.getLogger("dsh-kb.ask")

_MAX_CITATIONS = 6
_CONTEXT_CHARS = 1000
_MAX_QUESTION = 500

_SYSTEM = (
    "你是个人知识库的问答助手。只依据用户提供的报告片段回答问题，不要引入片段之外的事实。"
    "回答用中文、分点（3-6 点），每条结论末尾用 [n] 标注来源编号；"
    "如果片段不足以回答，就直接说明缺少哪些信息。"
    "只输出一个 JSON 对象：{\"answer\": \"...\"}，不要输出其他文字。"
)


def answer(question: str, *, limit: int = _MAX_CITATIONS) -> dict:
    text = (question or "").strip()
    if not text:
        return {"ok": False, "error": "问题不能为空"}
    if not kb_ai.is_configured():
        return {"ok": False, "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）"}
    kb_store.sync()
    result = kb_search.search(text, limit=max(1, min(12, limit)), offset=0, mode="auto")
    items = result["items"][:_MAX_CITATIONS]
    if not items:
        return {
            "ok": True,
            "answer": "知识库里暂时没有检索到与这个问题相关的报告。",
            "citations": [],
            "related": [],
            "mode": result["mode"],
        }

    citations = []
    blocks = []
    for number, item in enumerate(items, 1):
        detail = kb_store.get_report(item["path"]) or {}
        summary = item.get("summary_ai") or item.get("summary") or ""
        preview = (detail.get("preview") or "")[:_CONTEXT_CHARS] or (item.get("snippet") or "")
        citations.append(
            {
                "n": number,
                "title": item["title"],
                "path": item["path"],
                "url": f"reports/{item['path']}",
                "date": item.get("date", ""),
                "category": item.get("category", ""),
                "score": item.get("score") or item.get("semantic_score") or 0,
            }
        )
        blocks.append(
            f"[{number}] 《{item['title']}》（{item.get('date', '')}，{item.get('category', '')}）\n"
            f"摘要：{summary}\n正文摘录：{preview}"
        )

    prompt = (
        "报告片段：\n\n" + "\n\n".join(blocks) + f"\n\n用户问题：{text}\n\n"
        f"请依据以上片段回答（最多 {_MAX_CITATIONS} 个来源），只输出 JSON。"
    )
    try:
        raw = kb_ai.chat_json(_SYSTEM, prompt)
    except kb_ai.KBAIError as exc:
        logger.warning("问答失败：%s", exc)
        return {"ok": False, "error": f"模型调用失败：{exc}"}
    answer_text = str(raw.get("answer") or "").strip()
    if not answer_text:
        return {"ok": False, "error": "模型没有返回答案"}
    related = [
        {
            "title": item["title"],
            "path": item["path"],
            "url": f"reports/{item['path']}",
            "date": item.get("date", ""),
            "category": item.get("category", ""),
            "summary": item.get("summary_ai") or item.get("summary") or "",
        }
        for item in items[:5]
    ]
    return {
        "ok": True,
        "answer": answer_text,
        "citations": citations,
        "related": related,
        "mode": result["mode"],
        "model": kb_ai.model_label(),
    }
