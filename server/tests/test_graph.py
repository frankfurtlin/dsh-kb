"""P11：实体共现边、主题页、PPR 第三路召回的回归用例。"""

from __future__ import annotations

from app.services import kb_graph, kb_store


def _seed(path: str, entities: list[dict], tags: list[str]) -> None:
    kb_store.set_ai(
        path,
        {"summary_ai": "合成摘要", "key_points": ["要点"], "entities": entities, "tags": tags},
        model="test-model",
    )


def _prepare(client) -> None:
    client.get("/api/kb")
    _seed(
        "20260101/a-share-report.html",
        [{"name": "中国铝业", "type": "公司"}, {"name": "电解铝", "type": "主题"}],
        ["A股", "铝业"],
    )
    _seed(
        "20251231/stock-601800.html",
        [{"name": "中国铝业", "type": "公司"}, {"name": "电解铝", "type": "主题"}],
        ["A股", "电解铝"],
    )
    kb_graph.build(force=True)


def test_entity_edges_built(client):
    _prepare(client)

    conn = kb_store.connect()
    try:
        rows = conn.execute(
            "SELECT source_slug, target_slug, weight FROM entity_edges"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "共现边没有生成"
    pairs = {(row["source_slug"], row["target_slug"]) for row in rows}
    assert ("中国铝业", "电解铝") in pairs or ("电解铝", "中国铝业") in pairs
    assert max(row["weight"] for row in rows) == 2

    adjacency, pages = kb_store._graph()
    assert "中国铝业" in adjacency
    assert "电解铝" in adjacency["中国铝业"]

    mass = kb_store.personalized_pagerank(["中国铝业"])
    assert mass["中国铝业"] > mass["电解铝"] > 0


def test_topic_pages_created(client):
    _prepare(client)

    detail = kb_graph.page_detail("topic-a股")
    assert detail is not None
    assert detail["kind"] == "topic"
    assert detail["report_count"] == 2
    assert "相关报告" in detail["body_md"]

    listing = kb_graph.page_list(kind="topic")
    assert listing["total"] >= 1
    assert any(item["slug"] == "topic-a股" for item in listing["items"])

    stats = kb_graph.stats()
    assert stats["topics"] >= 1
    assert stats["edges"] >= 1


def test_ppr_report_boost(client):
    _prepare(client)

    boost = kb_store.ppr_report_boost(["20260101/a-share-report.html"])
    assert boost, "PPR 没有把页质量摊回报告"
    assert max(boost.values()) == 1.0
    assert set(boost) <= {"20260101/a-share-report.html", "20251231/stock-601800.html"}
