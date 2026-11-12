"""P0/P1 工程化行为：版本、静态缓存、gzip、统一错误体、健康检查。"""

from __future__ import annotations

import json
from pathlib import Path

from app import config

PACKAGE_JSON = Path(__file__).resolve().parents[1].parent / "package.json"


def test_healthz_version_matches_package_json(client):
    declared = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"]
    body = client.get("/healthz").json()
    assert body["version"] == declared
    assert body["uptime_seconds"] >= 0


def test_page_uses_fingerprinted_assets_and_no_cache(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    html = response.text
    for asset in ("style.css", "core.js", "ui.js", "kb.js"):
        assert f"static/{asset}?v={config.ASSET_VERSION}" in html
    assert f"v{config.APP_VERSION}" in html
    # 自定义日期区间与健康检查时代的 UI 元素
    assert 'id="kb-from"' in html and 'id="kb-to"' in html


def test_static_cache_policy(client):
    versioned = client.get("/static/kb.js", params={"v": "abc123"})
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers["cache-control"]
    assert "max-age=31536000" in versioned.headers["cache-control"]

    plain = client.get("/static/kb.js")
    assert "max-age=300" in plain.headers["cache-control"]

    prefixed = client.get("/kb/static/style.css", params={"v": "abc123"})
    assert prefixed.status_code == 200
    assert "immutable" in prefixed.headers["cache-control"]


def test_json_response_is_gzipped(client):
    response = client.get("/api/kb", params={"limit": 3}, headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    # 小于 500 字节的响应不压；这里三篇报告的 JSON 一定超过阈值
    assert response.headers.get("content-encoding") == "gzip"


def test_unknown_path_has_unified_error_body(client):
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "Not Found"}


def test_wrong_method_has_unified_error_body(client):
    response = client.post("/api/kb")
    assert response.status_code == 405
    assert response.json()["ok"] is False
    assert isinstance(response.json()["error"], str)


def test_validation_error_is_localized(client):
    response = client.post("/api/kb/state", json={"read": "not-a-bool"})
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"].startswith("参数不合法")
