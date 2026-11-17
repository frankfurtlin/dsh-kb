"""知识图谱节点编译与 /api/graph/* 接口的回归用例（P12 从 wiki 拆出）。"""

from __future__ import annotations

import json

from app import config
from app.services import kb_graph, kb_store

ENTITY = {"name": "中国铝业", "type": "公司"}
OTHER = {"name": "电解铝", "type": "主题"}


def _seed(path: str, entities: list[dict], tags: list[str]) -> None:
    kb_store.set_ai(
        path,
        {
            "summary_ai": "合成摘要",
            "key_points": ["要点一"],
            "entities": entities,
            "tags": tags,
        },
        model="test-model",
    )


def test_compile_builds_pages_and_links(client):
    client.get("/api/kb")  # 触发一次同步
    _seed("20260101/a-share-report.html", [ENTITY, OTHER], ["A股"])
    _seed("20251231/stock-601800.html", [ENTITY, OTHER], ["铝业"])
    result = kb_graph.build(force=True)

    assert result["pages"] >= 1
    page = kb_graph.page_detail("中国铝业")
    assert page is not None
    assert page["title"] == "中国铝业"
    assert page["report_count"] == 2
    assert {source["path"] for source in page["sources"]} == {
        "20260101/a-share-report.html",
        "20251231/stock-601800.html",
    }
    assert page["body_md"].startswith("---")
    assert "[[report:20260101/a-share-report.html|" in page["body_md"]
    assert len(page["links"]) >= 1

    # markdown 落盘（Obsidian 可直接打开）
    files = sorted(p.name for p in (config.GRAPH_DIR / "pages").glob("*.md"))
    assert "中国铝业.md" in files
    assert (config.GRAPH_DIR / "index.md").exists()
    assert (config.GRAPH_DIR / "SCHEMA.md").exists()


def test_graph_api_nodes_lint_and_html(client):
    client.get("/api/kb")
    _seed("20260101/a-share-report.html", [ENTITY], ["A股"])
    _seed("20251231/stock-601800.html", [ENTITY], ["铝业"])
    kb_graph.build(force=True)

    stats = client.get("/api/graph/stats").json()
    assert stats["pages"] >= 1
    assert stats["sources"] >= 1

    listing = client.get("/api/graph/nodes?limit=5").json()
    assert listing["total"] >= 1
    assert any(item["slug"] == "中国铝业" for item in listing["items"])

    lint = client.get("/api/graph/lint").json()
    assert lint["ok"] is True
    assert lint["counts"]["orphan_pages"] == 0
    assert lint["counts"]["broken_links"] == 0

    detail = client.get("/api/graph/node/中国铝业").json()
    assert detail["slug"] == "中国铝业"

    page_html = client.get("/graph/中国铝业")
    assert page_html.status_code == 200
    assert "<h1>中国铝业</h1>" in page_html.text
    assert 'href="/graph/' in page_html.text or 'href="/reports/' in page_html.text

    index_html = client.get("/graph/")
    assert index_html.status_code == 200
    assert "知识图谱" in index_html.text

    assert client.get("/api/graph/node/不存在").status_code == 404


def test_incremental_skip_and_compile_endpoint(client):
    client.get("/api/kb")
    _seed("20260101/a-share-report.html", [ENTITY], ["A股"])
    first = kb_graph.build(force=True)
    second = kb_graph.build()
    assert second["written"] == 0
    assert second["skipped"] == first["pages"]

    resp = client.post("/api/graph/compile", json={"force": True})
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["written"] == payload["pages"]


def test_removed_report_removes_page(client):
    client.get("/api/kb")
    # 用一份临时报告，避免删掉其它用例共用的合成报告（之前删 20260115/xhs-note.html
    # 会污染后面的 public 用例，属全量跑才暴露的隔离 bug）。
    temp_rel = "20260120/temp-only.html"
    temp_path = config.REPORTS_DIR / temp_rel
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(
        "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>临时报告</title></head>"
        "<body><h1>临时报告</h1><p>只用于验证删报告后节点会消失，正文含『仅此一篇』。</p></body></html>",
        encoding="utf-8",
    )
    client.get("/api/kb?refresh=1")  # 让新文件进索引，set_ai 才有行可写
    _seed(temp_rel, [{"name": "仅此一篇", "type": "主题"}], ["x"])
    # 只有一篇 → 低于 WIKI_MIN_REPORTS（默认 2），不应建页
    kb_graph.build(force=True)
    assert kb_graph.page_detail("仅此一篇") is None

    _seed("20260101/a-share-report.html", [{"name": "仅此一篇", "type": "主题"}], [])
    kb_graph.build(force=True)
    assert kb_graph.page_detail("仅此一篇") is not None

    temp_path.unlink()
    client.get("/api/kb?refresh=1")
    kb_graph.build()
    assert kb_graph.page_detail("仅此一篇") is None
