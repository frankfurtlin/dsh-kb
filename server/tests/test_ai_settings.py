"""v1.0.1：模型可选（从 DSH 模型列表取）、可记住、不可用即跳过。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest


class _StubBridge(BaseHTTPRequestHandler):
    catalog = {
        "providers": [
            {"id": "p-alpha", "name": "Alpha", "models": [{"id": "m-1", "name": "M1"}, {"id": "m-2", "name": "M2"}]},
            {"id": "p-beta", "name": "Beta", "models": [{"id": "m-3", "name": "M3"}]},
        ],
        "default": {"provider": "p-alpha", "model": "m-1"},
    }

    def log_message(self, *args):  # 静音
        pass

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.endswith("/models"):
            self._send({"ok": True, **self.catalog})
            return
        if parsed.path.endswith("/status"):
            query = parse_qs(parsed.query)
            provider = (query.get("provider") or [""])[0]
            model = (query.get("model") or [""])[0]
            if provider or model:
                known = any(
                    p["id"] == provider and any(m["id"] == model for m in p["models"])
                    for p in self.catalog["providers"]
                )
                if not known:
                    self._send({"ok": True, "available": False, "code": "MODEL_UNAVAILABLE",
                                "provider": provider, "model": model,
                                "reason": f"模型不可用：{provider}/{model}"})
                    return
                self._send({"ok": True, "available": True, "provider": provider, "model": model})
                return
            self._send({"ok": True, "available": True, "provider": "p-alpha", "model": "m-1"})
            return
        self._send({"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        provider = body.get("provider", "")
        model = body.get("model", "")
        known = any(
            p["id"] == provider and any(m["id"] == model for m in p["models"])
            for p in self.catalog["providers"]
        )
        if provider and model and not known:
            self._send({"ok": False, "code": "MODEL_UNAVAILABLE",
                        "error": f"模型不可用：{provider}/{model}"})
            return
        self._send({"ok": True, "text": '{"ok": true}', "provider": provider or "p-alpha",
                    "model": model or "m-1"})


@pytest.fixture()
def bridge(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubBridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    from app import config

    monkeypatch.setattr(config, "AI_URL", f"http://127.0.0.1:{server.server_port}/kb/_internal/ai")
    config.AI_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.AI_SECRET_FILE.write_text("x" * 64, encoding="utf-8")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        config.AI_SECRET_FILE.unlink(missing_ok=True)


def test_catalog_lists_dsh_models(client, bridge):
    data = client.get("/api/kb/ai").json()
    assert data["ok"] is True
    ids = [p["id"] for p in data["providers"]]
    assert ids == ["p-alpha", "p-beta"]
    assert data["default"]["provider"] == "p-alpha"


def test_save_and_read_selection(client, bridge):
    from app.services import kb_ai

    resp = client.post("/api/kb/ai", json={"provider": "p-beta", "model": "m-3"}).json()
    assert resp["ok"] is True
    assert resp["selected"] == {"provider": "p-beta", "model": "m-3"}
    assert kb_ai.selected() == {"provider": "p-beta", "model": "m-3"}

    again = client.get("/api/kb/ai").json()
    assert again["available"] is True
    assert again["selected"] == {"provider": "p-beta", "model": "m-3"}
    assert again["provider"] == "p-beta" and again["model"] == "m-3"


def test_unknown_model_rejected(client, bridge):
    resp = client.post("/api/kb/ai", json={"provider": "p-beta", "model": "nope"}).json()
    assert resp["ok"] is False and "不可用" in resp["error"]


def test_saved_model_disappears_means_unavailable_and_workers_skip(client, bridge, caplog):
    from app.services import kb_ai, kb_enrich, kb_store

    kb_store.set_setting("ai_selection", json.dumps({"provider": "gone", "model": "gone-1"}))
    kb_ai._status_cache = None

    data = client.get("/api/kb/ai").json()
    assert data["available"] is False
    assert "不可用" in data["reason"]

    caplog.clear()
    with caplog.at_level("WARNING"):
        result = kb_enrich.enrich_pending(limit=1)
    assert result["ok"] is False and result["skipped"] is True
    assert not [r for r in caplog.records if "模型调用失败" in r.message]


def test_reset_returns_to_default(client, bridge):
    from app.services import kb_ai

    client.post("/api/kb/ai", json={"provider": "p-alpha", "model": "m-2"})
    assert kb_ai.selected() is not None
    resp = client.post("/api/kb/ai", json={"provider": "", "model": ""}).json()
    assert resp["ok"] is True and resp["selected"] is None
    assert kb_ai.selected() is None
