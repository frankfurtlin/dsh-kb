"""LLM Wiki（P12 与图谱拆分后的占位层）接口契约。"""


def test_wiki_articles_empty(client):
    payload = client.get("/api/wiki/articles").json()
    assert payload["total"] == 0
    assert payload["mode"] == "disabled"


def test_wiki_stats_and_lint(client):
    stats = client.get("/api/wiki/stats").json()
    assert stats["articles"] == 0
    assert stats["mode"] == "disabled"
    lint = client.get("/api/wiki/lint").json()
    assert lint["stale"] == []


def test_wiki_article_missing_and_compile_disabled(client):
    assert client.get("/api/wiki/article/none").status_code == 404
    resp = client.post("/api/wiki/compile")
    assert resp.status_code == 501
    assert resp.json()["ok"] is False


def test_wiki_placeholder_page(client):
    page = client.get("/wiki/")
    assert page.status_code == 200
    assert "LLM Wiki" in page.text
    assert "知识图谱" in page.text
    assert client.get("/wiki/不存在").status_code == 404
