"""索引 / 列表 / 检索 / 详情 / 状态 的行为测试（HTTP 层，走真实 SQLite）。"""

from __future__ import annotations


def test_healthz_reports_counts(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["reports"]["total"] == 3
    assert body["reports"]["unread"] == 3
    assert body["reports"]["starred"] == 0
    assert body["ai"]["configured"] is False
    assert body["embed"]["enabled"] is False


def test_tree_groups_by_category_series_date(client):
    tree = client.get("/api/kb/tree").json()
    assert tree["total"] == 3
    categories = {row["name"]: row["count"] for row in tree["categories"]}
    assert categories == {"市场全景": 1, "话题精读": 1, "个股分析": 1}
    series = {(row["category"], row["name"]): row["count"] for row in tree["series"]}
    assert ("市场全景", "A股市场全景") in series
    assert ("个股分析", "中国铝业(601600)") in series
    assert ("话题精读", "小红书爆款笔记") in series
    assert {row["date"] for row in tree["dates"]} == {"2025-12-31", "2026-01-01", "2026-01-15"}


def test_list_filters_by_category_and_searches_title(client):
    only_market = client.get("/api/kb", params={"category": "市场全景"}).json()
    assert only_market["total"] == 1
    assert "A股市场全景" in only_market["items"][0]["title"]

    hit = client.get("/api/kb", params={"q": "铝业"}).json()
    assert hit["total"] == 1
    assert hit["items"][0]["match"] in ("title", "body")
    assert "铝业" in hit["items"][0]["snippet"]


def test_list_date_range_accepts_iso_and_compact(client):
    iso = client.get("/api/kb", params={"date_from": "2026-01-01", "date_to": "2026-01-31"}).json()
    assert iso["total"] == 2
    assert {item["date"] for item in iso["items"]} == {"2026-01-01", "2026-01-15"}

    compact = client.get("/api/kb", params={"date_from": "20260101", "date_to": "20260131"}).json()
    assert compact["total"] == 2

    half_open = client.get("/api/kb", params={"date_from": "2026-01-10"}).json()
    assert {item["date"] for item in half_open["items"]} == {"2026-01-15"}
    assert half_open["total"] == 1


def test_list_pagination_and_sort(client):
    page = client.get("/api/kb", params={"limit": 2, "offset": 0, "sort": "date_asc"}).json()
    assert page["total"] == 3
    assert [item["date"] for item in page["items"]] == ["2025-12-31", "2026-01-01"]
    page2 = client.get("/api/kb", params={"limit": 2, "offset": 2, "sort": "date_asc"}).json()
    assert [item["date"] for item in page2["items"]] == ["2026-01-15"]


def test_report_detail_has_preview_and_ai_fields(client):
    item = client.get("/api/kb/report/20251231/stock-601800.html").json()["item"]
    assert item["title"].startswith("中国铝业(601600)")
    assert "资产负债率" in item["preview"]
    assert item["read"] is False and item["starred"] is False
    assert item["summary_ai"] == ""


def test_missing_report_returns_unified_error(client):
    response = client.get("/api/kb/report/20260101/nope.html")
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "报告不存在或尚未建立索引"}


def test_state_read_and_starred_round_trip(client):
    as_read = client.post("/api/kb/state", json={"path": "20260101/a-share-report.html", "read": True}).json()
    assert as_read["read"] is True and as_read["starred"] is False

    starred = client.post("/api/kb/state", json={"path": "20260115/xhs-note.html", "starred": True}).json()
    assert starred["starred"] is True

    tree = client.get("/api/kb/tree").json()
    assert tree["unread"] == 2
    assert tree["starred"] == 1
    star_view = client.get("/api/kb", params={"view": "starred"}).json()
    assert star_view["total"] == 1
    unread_view = client.get("/api/kb", params={"view": "unread"}).json()
    assert unread_view["total"] == 2


def test_state_rejects_traversal_and_non_html(client):
    bad = client.post("/api/kb/state", json={"path": "../etc/passwd.html", "read": True})
    assert bad.status_code == 400
    assert bad.json()["ok"] is False

    not_html = client.post("/api/kb/state", json={"path": "assets/shell.css", "read": True})
    assert not_html.status_code == 400


def test_lexical_search_matches_body_keyword(client):
    body = client.get("/api/kb/search", params={"q": "资产负债率", "mode": "lexical"}).json()
    assert body["total"] == 1
    assert body["items"][0]["path"] == "20251231/stock-601800.html"
    assert body["items"][0]["match"] == "body"
    assert "资产负债率" in body["items"][0]["snippet"]


def test_hybrid_search_falls_back_to_lexical_without_vectors(client):
    body = client.get("/api/kb/search", params={"q": "铝业", "mode": "hybrid"}).json()
    assert body["total"] == 1
    assert body["mode"] in ("lexical", "hybrid")  # 向量未启用时降级，但不能报错
