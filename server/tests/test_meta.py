"""v0.3：入库/索引时间、AI 模型溯源、标签关系表、变更轨迹。"""

from __future__ import annotations


def _first_path(client) -> str:
    return client.get("/api/kb").json()["items"][0]["path"]


def test_items_expose_timestamps(client):
    item = client.get("/api/kb").json()["items"][0]
    assert item["created_at"] and item["updated_at"]
    assert item["read_at"] == ""
    assert item["ai_model"] == ""


def test_ai_model_and_tags_recorded(client):
    from app.services import kb_store

    target = _first_path(client)
    kb_store.set_ai(
        target,
        {
            "summary_ai": "结构化摘要",
            "key_points": ["要点一"],
            "entities": [],
            "tags": ["A股", "#半导体", " A股 "],
        },
        model="test/model",
    )

    detail = client.get(f"/api/kb/report/{target}").json()["item"]
    assert detail["ai_model"] == "test/model"
    assert detail["tags"] == ["A股", "半导体"]

    tags = {row["name"]: row["reports"] for row in client.get("/api/kb/tags").json()["tags"]}
    assert tags["A股"] == 1
    assert tags["半导体"] == 1

    filtered = client.get("/api/kb", params={"tag": "A股"}).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["path"] == target

    tree = client.get("/api/kb/tree", params={"tag": "A股"}).json()
    assert tree["total"] == 1
    assert any(row["name"] == "A股" for row in tree["tags"])


def test_retagging_replaces_old_links(client):
    from app.services import kb_store

    path = _first_path(client)
    kb_store.set_ai(path, {"summary_ai": "s", "tags": ["旧标签"]}, model="m")
    names = [row["name"] for row in client.get("/api/kb/tags").json()["tags"]]
    assert "旧标签" in names

    kb_store.set_ai(path, {"summary_ai": "s", "tags": ["新标签"]})
    names = [row["name"] for row in client.get("/api/kb/tags").json()["tags"]]
    assert "旧标签" not in names
    assert "新标签" in names


def test_state_and_public_record_time_and_events(client):
    path = _first_path(client)
    client.post("/api/kb/state", json={"path": path, "read": True, "starred": True})
    detail = client.get(f"/api/kb/report/{path}").json()["item"]
    assert detail["read_at"] and detail["starred_at"]

    client.post("/api/kb/public", json={"path": path, "public": True})
    detail = client.get(f"/api/kb/report/{path}").json()["item"]
    assert detail["public"] is True and detail["public_at"]

    events = client.get("/api/kb/events", params={"path": path}).json()["events"]
    kinds = [event["kind"] for event in events]
    assert "state" in kinds
    assert "public" in kinds
    assert all(event["path"] == path for event in events)
