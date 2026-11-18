"""LLM Wiki 文章层（P13）回归用例：生成、hash 跳过、API/HTML、lint、编译入口。"""

from __future__ import annotations

import json

from app import config
from app.services import kb_ai, kb_store, kb_wiki

TAG = "共现主题"


def _seed_all() -> None:
    payload = {
        "summary_ai": "合成摘要",
        "key_points": ["要点"],
        "entities": [{"name": "中国铝业", "type": "公司"}],
        "tags": [TAG],
    }
    for path in ("20260101/a-share-report.html", "20260115/xhs-note.html", "20251231/stock-601800.html"):
        kb_store.set_ai(path, payload, model="test-model")


def _fake_chat_json(*_args, **_kwargs) -> dict:
    return {
        "title": "共现主题 Wiki",
        "overview": "这是一篇由合成测试数据生成的文章。",
        "sections": [
            {"heading": "市场概览", "body": "上证指数上涨，来源见 [1]。"},
            {"heading": "个股视角", "body": "中国铝业资产负债率下降，来源见 [2]。"},
        ],
        "takeaways": ["结论一", "结论二"],
    }


def _enable_ai(monkeypatch) -> None:
    monkeypatch.setattr(kb_ai, "is_configured", lambda: True)
    monkeypatch.setattr(kb_ai, "model_label", lambda: "test-model · dsh-agent")
    monkeypatch.setattr(kb_ai, "chat_json", _fake_chat_json)


def test_generate_article_and_api(client, monkeypatch):
    client.get("/api/kb")  # 同步
    _seed_all()
    _enable_ai(monkeypatch)

    result = kb_wiki.generate(f"topic-{TAG}")
    assert result["ok"] is True
    assert result["sources"] == 3
    slug = f"topic-{TAG}"
    article = kb_wiki.article_detail(slug)
    assert article["title"] == "共现主题 Wiki"
    assert article["source_count"] == 3
    assert [s["n"] for s in article["sources"]] == [1, 2, 3]
    assert "[1]" in article["body_md"]

    listing = client.get("/api/wiki/articles").json()
    assert listing["total"] == 1
    assert listing["items"][0]["slug"] == slug
    assert client.get(f"/api/wiki/article/{slug}").json()["slug"] == slug
    assert client.get("/api/wiki/stats").json()["articles"] == 1

    index = client.get("/wiki/")
    assert index.status_code == 200
    assert "共现主题 Wiki" in index.text
    page = client.get(f"/wiki/{slug}")
    assert page.status_code == 200
    assert "来源" in page.text
    assert "src-1" in page.text

    # 文章 markdown 已落盘
    assert (config.WIKI_DIR / "articles" / f"{slug}.md").exists()
    assert (config.WIKI_DIR / "index.md").exists()


def test_generate_skips_when_hash_unchanged(client, monkeypatch):
    client.get("/api/kb")
    _seed_all()
    _enable_ai(monkeypatch)
    first = kb_wiki.generate(f"topic-{TAG}")
    assert first["ok"] is True
    second = kb_wiki.generate(f"topic-{TAG}")
    assert second["ok"] is True
    assert second["skipped"] is True


def test_compile_endpoint_and_disabled_mode(client, monkeypatch):
    # AI 未配置：compile 返回 503（与 501 占位区分）
    resp = client.post("/api/wiki/compile")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False

    client.get("/api/kb")
    _seed_all()
    _enable_ai(monkeypatch)
    resp = client.post("/api/wiki/compile", json={"limit": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["generated"]) == 1


def test_lint_reports_missing_or_stale(client, monkeypatch):
    client.get("/api/kb")
    _seed_all()
    _enable_ai(monkeypatch)
    kb_wiki.generate(f"topic-{TAG}")
    lint = client.get("/api/wiki/lint").json()
    assert lint["checked"] == 1
    assert lint["stale"] == []
    assert lint["empty"] == []


def test_article_html_404_and_bad_slug(client, monkeypatch):
    client.get("/api/kb")
    _seed_all()
    _enable_ai(monkeypatch)
    assert client.get("/wiki/不存在").status_code == 404
    assert client.get("/api/wiki/article/不存在").status_code == 404
    # 候选不足的 slug 不能生成
    try:
        kb_wiki.generate("topic-不存在的主题")
    except kb_wiki.WikiError as exc:
        assert "没有可达标的候选" in str(exc)
    else:  # pragma: no cover - 不允许静默通过
        raise AssertionError("应当抛出 WikiError")


def test_wiki_index_hides_per_card_regenerate_for_live_articles(client, monkeypatch):
    """P1-4：索引页只给过期文章留「重新生成」，正常文章点进详情页再操作。"""
    client.get("/api/kb")  # 同步
    _seed_all()
    _enable_ai(monkeypatch)
    slug = f"topic-{TAG}"
    assert kb_wiki.generate(slug)["ok"] is True

    html = client.get("/wiki/").text
    assert "打开 →" in html
    # 索引页顶部只有一个「生成一篇」（gen('', ...)）；正常文章不应再挂按钮。
    assert html.count('onclick="gen(') == 1

    kb_store.mark_wiki_article_stale(slug)
    html = client.get("/wiki/").text
    assert html.count('onclick="gen(') == 2
    assert "来源已变 · 建议重新生成" in html
