"""v1.0：报告附件（图片/CSS/数据文件）进内容寻址存储，按原路径输出。"""

from __future__ import annotations

import hashlib


def _put_asset(client, rel: str, data: bytes, mime: str = "image/png") -> str:
    from app.services import kb_blob, kb_store

    blob = kb_blob.put(data, mime, rel)
    kb_store.put_asset(rel, blob["sha"], mime, len(data))
    return blob["sha"]


def test_private_asset_served_from_store(client):
    data = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    _put_asset(client, "20260101/images/demo/pic.png", data)

    resp = client.get("/kb/reports/20260101/images/demo/pic.png")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"].startswith("image/png")
    # 磁盘上确实没有这个文件，证明是从库里取出来的
    from app import config
    from pathlib import Path

    assert not (Path(config.REPORTS_DIR) / "20260101/images/demo/pic.png").exists()


def test_public_asset_allowed_for_referenced_file(client):
    """公开报告 HTML 里引用到的附件，才能在 /kb-public 下匿名取。"""
    import datetime as dt

    from app.services import kb_blob, kb_clip, kb_store

    _put_asset(client, "20260102/images/demo/plate.png", b"plate-bytes")
    html = (
        '<!doctype html><html><head><title>公开图册</title></head>'
        '<body><img src="images/demo/plate.png"></body></html>'
    )
    doc = client.post(
        "/api/kb/ingest",
        json={"text": html, "title": "公开图册", "published_at": "2026-01-02"},
    ).json()["document"]
    # 模拟「文件型报告迁移后保留原路径」：把合成路径改成 20260102/gallery.html
    rel_path = "20260102/gallery.html"
    conn = kb_store._connect()
    try:
        conn.execute("UPDATE reports SET path = ? WHERE uid = ?", (rel_path, doc["uid"]))
        conn.commit()
    finally:
        conn.close()
    # 私有 → 404
    assert client.get("/kb-public/reports/20260102/images/demo/plate.png").status_code == 404

    kb_store.set_public(rel_path, True)
    resp = client.get("/kb-public/reports/20260102/images/demo/plate.png")
    assert resp.status_code == 200
    assert resp.content == b"plate-bytes"


def test_asset_migration_is_idempotent(tmp_path, monkeypatch):
    from pathlib import Path

    from app import config
    from app.services import kb_blob, kb_store

    root = tmp_path / "reports"
    (root / "20260103" / "images").mkdir(parents=True)
    (root / "20260103" / "images" / "a.png").write_bytes(b"aaa")
    (root / "20260103" / "notes.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPORTS_DIR", root)

    from app import cli_migrate_assets

    result = cli_migrate_assets.migrate(apply=True)
    assert result["moved"] == 2 and result["errors"] == []
    assert not any(p.is_file() for p in root.rglob("*"))
    row = kb_store.get_asset("20260103/images/a.png")
    assert row and kb_blob.get(row["sha"]) == b"aaa"
    # 再跑一次：没有文件可迁，也不报错
    assert cli_migrate_assets.migrate(apply=True)["moved"] == 0
