"""P6 公开外链：匿名访问边界、内链改写、目录页与缓存头。"""

from __future__ import annotations

REPORT = "20260101/a-share-report.html"
PRIVATE_REPORT = "20260115/xhs-note.html"


def _publish(client, path: str) -> dict:
    body = client.post("/api/kb/public", json={"path": path, "public": True}).json()
    assert body["ok"] is True
    return body


def test_publish_returns_public_url_and_tree_count(client):
    body = _publish(client, REPORT)
    assert body["url"] == f"https://example.test/kb-public/reports/{REPORT}"
    assert client.get("/api/kb/public").json()["paths"] == [REPORT]
    assert client.get("/api/kb/tree").json()["public"] == 1


def test_public_report_is_anonymous_and_rewrites_links(client):
    _publish(client, REPORT)
    response = client.get(f"/kb-public/reports/{REPORT}")
    assert response.status_code == 200
    html = response.text
    assert "A股市场全景分析报告" in html
    # 站内绝对链接与共享静态资源都改写到公开前缀
    assert 'href="/kb-public/reports/20260115/xhs-note.html"' in html
    assert 'href="/kb-public/reports/assets/shell.css"' in html
    assert '"/kb/' not in html
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_private_report_and_api_are_not_exposed(client):
    assert client.get(f"/kb-public/reports/{PRIVATE_REPORT}").status_code == 404
    assert client.get("/kb-public/api/kb/tree").status_code == 404


def test_shared_asset_requires_existing_file(client):
    assert client.get("/kb-public/reports/assets/shell.css").status_code == 200
    assert client.get("/kb-public/reports/assets/nope.css").status_code == 404


def test_public_index_lists_only_published_reports(client):
    empty = client.get("/kb-public/")
    assert empty.status_code == 200
    assert "还没有公开" in empty.text

    _publish(client, REPORT)
    listed = client.get("/kb-public/").text
    assert "A股市场全景分析报告" in listed
    assert "xhs-note" not in listed
    assert 'content="noindex, nofollow"' in listed


def test_shared_asset_and_report_assets_allowed(client):
    _publish(client, REPORT)
    asset = client.get("/kb-public/reports/assets/shell.css")
    assert asset.status_code == 200
    assert "max-age=300" in asset.headers["cache-control"]
    assert asset.headers["x-robots-tag"].startswith("noindex")


def test_public_paths_reject_traversal(client):
    _publish(client, REPORT)
    assert client.get("/kb-public/reports/../app/config.py").status_code == 404
    assert client.get("/kb-public/reports/%2e%2e/%2e%2e/app/config.py").status_code == 404


def test_unpublish_hides_report_immediately(client):
    _publish(client, REPORT)
    assert client.get(f"/kb-public/reports/{REPORT}").status_code == 200
    body = client.post("/api/kb/public", json={"path": REPORT, "public": False}).json()
    assert body["public"] is False
    assert client.get(f"/kb-public/reports/{REPORT}").status_code == 404


def test_public_root_requires_trailing_slash(client):
    response = client.get("/kb-public", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/kb-public/")


def test_publish_rejects_bad_paths(client):
    bad = client.post("/api/kb/public", json={"path": "../secrets.html", "public": True})
    assert bad.status_code == 400
    assert bad.json()["ok"] is False
    missing = client.post("/api/kb/public", json={"path": "20260101/nope.html", "public": True})
    assert missing.status_code == 404


def test_import_legacy_state(tmp_path, client):
    import json

    legacy = tmp_path / "kb_public.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    REPORT: {"public": True, "updated_at": 1},
                    "20260101/gone.html": {"public": True, "updated_at": 1},
                    PRIVATE_REPORT: {"public": False, "updated_at": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    from app.services import kb_public, kb_store

    result = kb_public.import_legacy_state(str(legacy))
    assert result["imported"] == 1
    assert result["missing"] == 1
    assert REPORT in kb_store.public_paths()
    assert client.get(f"/kb-public/reports/{REPORT}").status_code == 200


def test_import_legacy_state_missing_file():
    from app.services import kb_public

    result = kb_public.import_legacy_state("/nonexistent/kb_public.json")
    assert "error" in result


def test_bulk_public(client):
    body = client.post(
        "/api/kb/bulk", json={"paths": [REPORT, PRIVATE_REPORT], "public": True}
    ).json()
    assert body["ok"] is True and body["updated"] == 2
    listed = client.get("/api/kb/public").json()["paths"]
    assert REPORT in listed and PRIVATE_REPORT in listed
    assert client.get(f"/kb-public/reports/{PRIVATE_REPORT}").status_code == 200

    body = client.post("/api/kb/bulk", json={"paths": [REPORT], "public": False}).json()
    assert body["updated"] == 1
    assert client.get(f"/kb-public/reports/{REPORT}").status_code == 404
