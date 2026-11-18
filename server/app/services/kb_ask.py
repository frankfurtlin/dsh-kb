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


def _prepare(question: str, *, limit: int = _MAX_CITATIONS, context_chars: int = _CONTEXT_CHARS) -> dict:
    """检索证据并拼出模型提示词；返回 {ok} 或 {ok:True, prompt, citations, related, mode}。"""
    text = (question or "").strip()
    if not text:
        return {"ok": False, "error": "问题不能为空"}
    if not kb_ai.is_configured():
        return {"ok": False, "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）"}
    kb_store.sync()
    # P0-1 之后 hybrid 模式要求词法命中，而自然语言提问往往一个词都不命中；
    # RAG 需要「宁可放宽召回、让模型判断相关性」，所以先用语义召回，再用混合兜底。
    result = kb_search.search(text, limit=max(_MAX_CITATIONS, 10), offset=0, mode="semantic")
    if not result["items"]:
        result = kb_search.search(text, limit=max(_MAX_CITATIONS, 10), offset=0, mode="hybrid")
    pool = result["items"]
    items = pool[:_MAX_CITATIONS]
    # 综述文章通常比单篇报告更适合回答跨报告问题：池子里有文章时，保底给一篇位置。
    if not any(item.get("kind") == "article" for item in items):
        best_article = next((item for item in pool if item.get("kind") == "article"), None)
        if best_article is not None:
            items = items[:_MAX_CITATIONS - 1] + [best_article]
    if not items:
        return {
            "ok": True,
            "empty": True,
            "answer": "知识库里暂时没有检索到与这个问题相关的报告。",
            "citations": [],
            "related": [],
            "mode": result["mode"],
        }

    citations = []
    blocks = []
    for number, item in enumerate(items, 1):
        if item.get("kind") == "article":
            article = kb_store.wiki_article_detail(item["slug"]) or {}
            overview = item.get("summary") or article.get("overview") or ""
            body = (article.get("body_md") or "").replace("---", " ")
            citations.append(
                {
                    "n": number,
                    "title": item["title"],
                    "path": item["path"],
                    "url": item["url"],
                    "date": item.get("date") or "",
                    "category": "LLM Wiki · 文章",
                    "score": item.get("score") or item.get("semantic_score") or 0,
                }
            )
            blocks.append(
                f"[{number}] 《{item['title']}》（LLM Wiki 综述，{item.get('source_count', 0)} 篇来源）\n"
                f"总览：{overview}\n正文节选：{body[:1200]}"
            )
            continue
        if item.get("kind") == "node":
            summary = item.get("summary") or ""
            citations.append(
                {
                    "n": number,
                    "title": item["title"],
                    "path": item["path"],
                    "url": item["url"],
                    "date": "",
                    "category": f"图谱 · {item.get('entity_type') or 'entity'}",
                    "score": item.get("score") or item.get("semantic_score") or 0,
                }
            )
            blocks.append(
                f"[{number}] 《{item['title']}》（Wiki 页，{item.get('report_count', 0)} 篇报告提及）\n"
                f"页摘要：{summary}"
            )
            continue
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

    summaries = {
        str(it.get("path") or ""): str(
            it.get("summary_ai") or it.get("summary") or it.get("overview") or ""
        )[:180]
        for it in items
    }
    for cite in citations:
        cite["summary"] = summaries.get(str(cite.get("path") or ""), "")

    prompt = (
        "报告片段：\n\n" + "\n\n".join(blocks) + f"\n\n用户问题：{text}\n\n"
        f"请依据以上片段回答（最多 {_MAX_CITATIONS} 个来源），只输出 JSON。"
    )
    related = [
        {
            "title": item["title"],
            "path": item["path"],
            "url": item.get("url") if item.get("kind") == "node" else f"reports/{item['path']}",
            "date": item.get("date", ""),
            "category": (
                f"图谱 · {item.get('entity_type')}"
                if item.get("kind") == "node"
                else item.get("category", "")
            ),
            "summary": item.get("summary_ai") or item.get("summary") or "",
            "kind": item.get("kind", "report"),
        }
        for item in items[:5]
    ]
    return {
        "ok": True,
        "prompt": prompt,
        "citations": citations,
        "related": related,
        "mode": result["mode"],
    }


def answer(question: str, *, limit: int = _MAX_CITATIONS) -> dict:
    """一次性问答：返回完整答案与引用。"""
    prepared = _prepare(question, limit=limit)
    if not prepared.get("ok"):
        return prepared
    if prepared.get("empty"):
        return prepared
    try:
        raw = kb_ai.chat_json(_SYSTEM, prepared["prompt"])
    except kb_ai.KBAIError as exc:
        logger.warning("问答失败：%s", exc)
        return {"ok": False, "error": f"模型调用失败：{exc}"}
    answer_text = str(raw.get("answer") or "").strip()
    if not answer_text:
        return {"ok": False, "error": "模型没有返回答案"}
    return {
        "ok": True,
        "answer": answer_text,
        "citations": prepared["citations"],
        "related": prepared["related"],
        "mode": prepared["mode"],
        "model": kb_ai.model_label(),
    }


_STREAM_SYSTEM = (
    "你是个人知识库的问答助手。只依据用户提供的报告片段回答问题，不要引入片段之外的事实。"
    "直接用中文分点作答（3-6 点），每条结论末尾用 [n] 标注来源编号，使用 Markdown 列表。"
    "如果片段不足以回答，就直接说明缺少哪些信息。不要输出 JSON，不要用代码块包裹。"
)


def answer_stream(question: str, *, limit: int = 4, context_chars: int = 700):
    """流式问答：先 citations，再 delta 文本，最后 done。

    模型这里不要求 JSON：直接让模型写 Markdown 列表，前端边收边渲染，
    首字延迟从十几秒降到 1–2 秒（取决于模型首 token 时间）。
    """
    prepared = _prepare(question, limit=limit, context_chars=context_chars)
    if not prepared.get("ok"):
        yield ("error", {"error": str(prepared.get("error") or "问答失败")})
        return
    if prepared.get("empty"):
        yield ("empty", {"answer": prepared["answer"]})
        return
    yield (
        "citations",
        {
            "citations": prepared["citations"],
            "related": prepared["related"],
            "mode": prepared["mode"],
            "model": kb_ai.model_label(),
        },
    )
    collected: list[str] = []
    try:
        for kind, payload in kb_ai.chat_stream(_STREAM_SYSTEM, prepared["prompt"]):
            if kind == "thinking":
                yield ("thinking", {"chars": payload})
            elif kind == "delta":
                collected.append(str(payload))
                yield ("delta", {"text": str(payload)})
            elif kind == "done":
                break
    except kb_ai.KBAIError as exc:
        logger.warning("流式问答失败：%s", exc)
        yield ("error", {"error": f"模型调用失败：{exc}"})
        return
    answer_text = "".join(collected).strip()
    if not answer_text:
        yield ("error", {"error": "模型没有返回答案"})
        return
    yield ("done", {"answer": answer_text, "model": kb_ai.model_label()})
