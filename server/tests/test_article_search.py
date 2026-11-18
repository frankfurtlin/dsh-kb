"""P13+：LLM Wiki 文章参与检索、问答证据与文章详情互链/时间线。"""

from __future__ import annotations

import numpy as np

from app.services import kb_store


def _fake_embedding(seed: int = 1) -> list[float]:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    return (vector / np.linalg.norm(vector)).tolist()


def _seed_article(client, slug: str, title: str, *, sources: list[dict], overview: str) -> None:
    client.get("/api/kb?refresh=1")
    kb_store.save_wiki_article(
        slug,
        title=title,
        kind="topic",
        subject=title,
        overview=overview,
        body_md=f"# {title}\n\n> LLM Wiki\n\n{overview}\n\n## 小节\n\n正文 [1]\n",
        source_hash=f"hash-{slug}",
        sources=sources,
        model="test-model",
        takeaways=1,
    )
    kb_store.set_article_embedding(slug, _fake_embedding(), "fake-embed")


def test_article_vectors_and_search(client):
    _seed_article(
        client,
        "topic-a股",
        "A股市场全景综述",
        sources=[
            {"path": "20251231/stock-601800.html", "title": "中国铝业(601600) 投资分析报告", "date": "2025-12-31"},
            {"path": "20260101/a-share-report.html", "title": "A股市场全景分析报告", "date": "2026-01-01"},
        ],
        overview="A股在 2026 年初维持结构性行情，半导体领涨、银行走弱。",
    )
    stats = kb_store.article_embed_stats("fake-embed")
    assert stats["embedded"] == 1

    items, total = kb_store.hybrid_search(
        "",
        query_vector=_fake_embedding(),
        model="fake-embed",
        include_nodes=False,
        include_articles=True,
        limit=10,
    )
    assert total >= 1
    article_items = [item for item in items if item.get("kind") == "article"]
    assert article_items, items
    assert article_items[0]["slug"] == "topic-a股"
    assert article_items[0]["category"] == "LLM Wiki"


def test_article_detail_related_and_timeline(client):
    sources_a = [
        {"path": "20251231/stock-601800.html", "title": "中国铝业(601600) 投资分析报告", "date": "2025-12-31"},
        {"path": "20260101/a-share-report.html", "title": "A股市场全景分析报告", "date": "2026-01-01"},
    ]
    _seed_article(client, "topic-a", "A 主题", sources=sources_a, overview="总览 A")
    _seed_article(
        client,
        "topic-b",
        "B 主题",
        sources=[sources_a[0], sources_a[1]],
        overview="总览 B",
    )
    detail = kb_store.wiki_article_detail("topic-a")
    assert detail is not None
    assert [item["slug"] for item in detail["related"]] == ["topic-b"]
    assert [item["date"] for item in detail["timeline"]] == ["2025-12-31", "2026-01-01"]


def test_graph_canvas_endpoints(client):
    client.get("/api/graph/compile")
    payload = client.get("/api/graph/graph?limit=20").json()
    assert payload["showing"] >= 0
    assert isinstance(payload["edges"], list)
    page = client.get("/graph/canvas")
    assert page.status_code == 200
    assert "kb-canvas" in page.text
