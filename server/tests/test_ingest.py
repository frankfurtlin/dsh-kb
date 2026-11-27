"""v1.0 P0/P1：发布时间、时间线导航、投喂入口与私有 blob。"""

from __future__ import annotations

import sqlite3

import pytest


# ---------- P0：published_at ----------


@pytest.mark.parametrize(
    "rel,title,expected",
    [
        ("20260101/a-share-report.html", "别的标题", "2026-01-01"),
        ("20260101/clip-foo.html", "A股市场全景分析报告 | 2026-01-02 收盘", "2026-01-02"),
        ("20260101/plain.html", "没有日期的标题", "2026-01-01"),
        ("20261340/bad.html", "标题也没日期", ""),
    ],
)
def test_published_for_prefers_filename_then_title_then_dir(rel, title, expected):
    from app.services import kb_index

    assert kb_index._published_for(rel, title) == expected


def test_tree_exposes_timeline_and_months(client):
    data = client.get("/api/kb/tree").json()
    keys = [row["key"] for row in data["timeline"]]
    assert keys == ["today", "week", "month", "year", "older", "all"]
    assert data["timeline"][-1]["count"] == data["total"]
    assert isinstance(data["months"], list)


def test_backfill_published_fills_file_rows(client):
    from app.services import kb_store

    result = kb_store.backfill_published()
    assert result["scanned"] >= 3
    conn = kb_store._connect()
    try:
        rows = conn.execute(
            "SELECT date, published_at FROM reports WHERE COALESCE(doc_kind,'file')='file'"
        ).fetchall()
    finally:
        conn.close()
    assert rows and all(r["published_at"] == r["date"] for r in rows)


# ---------- P1：投喂入口 ----------


def test_ingest_text_creates_db_document(client):
    resp = client.post(
        "/api/kb/ingest",
        json={"text": "# 投喂测试\n\n一段用于验证入库的正文，不落盘。", "title": "投喂测试", "tags": ["测试"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    doc = body["document"]
    assert doc["doc_kind"] == "doc" and doc["uid"]
    assert doc["url"] == f"doc/{doc['uid']}"
    assert doc["path"] == f"doc/{doc['uid']}.html"

    listing = client.get("/api/kb").json()
    assert any(item["uid"] == doc["uid"] for item in listing["items"])

    detail = client.get(f"/api/kb/doc/{doc['uid']}").json()["item"]
    assert detail["title"] == "投喂测试"

    page = client.get(f"/kb/doc/{doc['uid']}")
    assert page.status_code == 200 and "投喂测试" in page.text


def test_ingest_dedupes_same_text(client):
    payload = {"text": "# 去重\n\n同一段内容投两次只应留一条。", "title": "去重测试"}
    first = client.post("/api/kb/ingest", json=payload).json()["document"]
    second = client.post("/api/kb/ingest", json=payload).json()["document"]
    assert first["uid"] == second["uid"]


def test_upload_multipart_creates_document(client):
    files = {"files": ("note.md", "# 上传\n\nmultipart 正文".encode("utf-8"), "text/markdown")}
    resp = client.post("/api/kb/upload", files=files, data={"title": "上传测试", "tags": "a,b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["imported"] == 1
    doc = body["items"][0]["document"]
    assert doc["mime"] == "text/markdown"
    assert doc["blob_sha"]


def test_ingested_doc_survives_sync(client):
    from app.services import kb_store

    doc = client.post("/api/kb/ingest", json={"text": "同步不该删掉它", "title": "sync 测试"}).json()["document"]
    kb_store.sync(force=True)
    assert kb_store.get_document(doc["uid"]) is not None


def test_blob_route_serves_bytes(client):
    body = "blob 正文".encode("utf-8")
    doc = client.post(
        "/api/kb/upload",
        files={"files": ("blob.txt", body, "text/plain")},
        data={"title": "blob 测试"},
    ).json()["items"][0]["document"]
    resp = client.get(f"/api/kb/blob/{doc['blob_sha']}")
    assert resp.status_code == 200 and resp.content == body


# ---------- 私有 blob：加密 / 阈值 ----------


def test_blob_encrypt_roundtrip():
    from app.services import kb_blob

    data = b"secret payload" * 10
    sha = kb_blob.sha256_hex(data)
    enc = kb_blob.encrypt(data, sha)
    assert enc.startswith(kb_blob.MAGIC)
    assert data not in enc
    assert kb_blob.decrypt(enc, sha) == data
    with pytest.raises(Exception):
        kb_blob.decrypt(enc, "0" * 64)  # AAD 绑定 sha，换 sha 必须解不开


def test_blob_local_roundtrip(monkeypatch):
    from app.services import kb_blob

    monkeypatch.setattr(kb_blob, "cos_upload", lambda *a, **k: False)
    data = b"local only"
    out = kb_blob.put(data, "text/plain", "x.txt")
    assert out["tier"] == "local"
    assert kb_blob.get(out["sha"]) == data


def test_large_blob_goes_to_cos(monkeypatch):
    from app.services import kb_blob

    calls = []

    def fake_upload(sha, data, mime=""):
        calls.append(len(data))
        return True

    monkeypatch.setattr(kb_blob, "cos_upload", fake_upload)
    monkeypatch.setattr(kb_blob, "cos_exists", lambda sha: True)
    big = b"x" * (kb_blob.COS_THRESHOLD + 1024)
    out = kb_blob.put(big, "application/octet-stream", "big.bin")
    assert out["tier"] == "cos" and out["mirrored"] is True
    assert calls and calls[0] > kb_blob.COS_THRESHOLD
    assert kb_blob.has_local(out["sha"]) is False


# ---------- P3：存量报告迁移 ----------


def test_migrate_rewrites_relative_assets_to_absolute():
    from app.cli_migrate_reports import rewrite_html

    html = (
        '<link rel="stylesheet" href="../assets/shell.css">'
        '<script src="../assets/shell.js"></script>'
        '<link rel="icon" href="../../static/icon-192.png">'
        '<img src="images/plate-01.png">'
    )
    out = rewrite_html(html, "20260101/report.html")
    assert 'href="/kb/reports/assets/shell.css"' in out
    assert 'src="/kb/reports/assets/shell.js"' in out
    assert 'href="/kb/static/icon-192.png"' in out
    assert 'src="/kb/reports/20260101/images/plate-01.png"' in out


def test_html_document_renders_in_sandboxed_iframe(client):
    from app.services import kb_blob, kb_store

    html = "<!doctype html><title>迁移报告</title><p>正文</p>".encode("utf-8")
    blob = kb_blob.put(html, "text/html", "legacy.html")
    doc = kb_store.create_document(
        title="迁移报告",
        body="正文",
        mime="text/html",
        blob_sha=blob["sha"],
        size=len(html),
        source="legacy-import",
    )
    page = client.get(f"/kb/doc/{doc['uid']}")
    assert page.status_code == 200 and "doc-frame" in page.text

    served = client.get(f"/api/kb/blob/{blob['sha']}")
    assert served.status_code == 200
    assert "sandbox" in served.headers.get("content-security-policy", "")
    assert served.content == html


# ---------- v1.0：剪藏直接入库（不再写 reports/） ----------


def _clip_item(url: str = "https://example.test/a") -> dict:
    return {
        "url": url,
        "site": "example.test",
        "platform": "generic",
        "ok": True,
        "title": "剪藏测试",
        "author": "",
        "error": "",
        "path": "",
        "uid": "",
        "doc_url": "",
        "updated": False,
        "chars": 0,
    }


def test_clip_persist_creates_document_then_updates_same_uid(client):
    import datetime as dt

    from app.services import kb_clip, kb_store

    item = _clip_item()
    html = "<!doctype html><html><head><title>剪藏测试</title></head><body><p>v1</p></body></html>"
    first = kb_clip._persist(item, body_text="第一版正文" * 10, html=html, when=dt.datetime.now())
    assert first["updated"] is False
    before = kb_store.get_document(first["uid"])
    assert before and before["source"] == "clip" and before["source_url"] == item["url"]

    second = kb_clip._persist(
        item,
        body_text="第二版正文" * 10,
        html=html.replace("<p>v1</p>", "<p>v2</p>"),
        when=dt.datetime.now(),
    )
    assert second["uid"] == first["uid"] and second["updated"] is True
    after = kb_store.get_document(first["uid"])
    assert after["blob_sha"] != before["blob_sha"]      # 正文换了新 blob
    assert after["ai_version"] == 0                      # 等后台重新增强
    conn = kb_store._connect()
    try:
        body = conn.execute("SELECT body FROM reports WHERE uid = ?", (first["uid"],)).fetchone()["body"]
    finally:
        conn.close()
    assert "第二版正文" in body


def test_clip_persist_keeps_one_row_per_url(client):
    import datetime as dt

    from app.services import kb_clip

    html = "<!doctype html><title>t</title>"
    for _ in range(3):
        kb_clip._persist(
            _clip_item("https://example.test/same"),
            body_text="同一链接重复剪藏" * 5,
            html=html,
            when=dt.datetime.now(),
        )
    items = client.get("/api/kb?limit=50").json()["items"]
    same = [it for it in items if it.get("source_url") == "https://example.test/same"]
    assert len(same) == 1


def test_clip_update_drops_stale_blob(client):
    import datetime as dt

    from app.services import kb_blob, kb_clip, kb_store

    item = _clip_item("https://example.test/blob-clean")
    first = kb_clip._persist(item, body_text="一" * 60, html="<html>1</html>", when=dt.datetime.now())
    before = kb_blob.meta(first["sha"])
    kb_clip._persist(item, body_text="二" * 60, html="<html>2</html>", when=dt.datetime.now())
    assert before is not None                      # 第一次的 blob 有台账
    assert kb_blob.meta(first["sha"]) is None      # 更新后旧 blob 被清理
    assert kb_store.count_reports_by_blob(first["sha"]) == 0
