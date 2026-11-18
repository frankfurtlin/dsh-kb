"""流式问答：事件顺序、桥未配置、SSE 端点格式。"""

from __future__ import annotations


def test_answer_stream_order_and_done(client, monkeypatch):
    from app.services import kb_ai, kb_ask

    monkeypatch.setattr(
        kb_ask,
        "_prepare",
        lambda question, limit=4, context_chars=700: {
            "ok": True,
            "citations": [],
            "related": [],
            "mode": "semantic",
            "prompt": "片段",
        },
    )
    monkeypatch.setattr(
        kb_ai,
        "chat_stream",
        lambda system, user: iter(
            [("thinking", 40), ("delta", "第一段"), ("delta", "第二段"), ("done", {"ok": True})]
        ),
    )
    events = list(kb_ask.answer_stream("问题"))
    assert [kind for kind, _ in events] == ["citations", "thinking", "delta", "delta", "done"]
    assert events[-1][1]["answer"] == "第一段第二段"


def test_stream_endpoint_unconfigured_is_503(client):
    response = client.post("/api/kb/ask/stream", json={"question": "铝业"})
    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_stream_endpoint_emits_sse_events(client, monkeypatch):
    from app.services import kb_ai, kb_ask

    monkeypatch.setattr(kb_ai, "is_configured", lambda: True)
    monkeypatch.setattr(
        kb_ask,
        "answer_stream",
        lambda question, limit=4: iter(
            [
                (
                    "citations",
                    {
                        "citations": [{"n": 1, "title": "报告", "path": "a/b.html", "url": "reports/a/b.html",
                                       "date": "2026-01-01", "category": "市场全景", "score": 0.9, "summary": "摘要"}],
                        "related": [],
                        "mode": "semantic",
                    },
                ),
                ("thinking", {"chars": 120}),
                ("delta", {"text": "- 结论 [1]"}),
                ("done", {"answer": "- 结论 [1]", "model": "test-model"}),
            ]
        ),
    )
    response = client.post("/api/kb/ask/stream", json={"question": "铝业"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    for event in ("citations", "thinking", "delta", "done"):
        assert f"event: {event}" in body
    assert "结论 [1]" in body


def test_answer_stream_error_event(client, monkeypatch):
    from app.services import kb_ai, kb_ask

    monkeypatch.setattr(
        kb_ask,
        "_prepare",
        lambda question, limit=4, context_chars=700: {"ok": False, "error": "AI 未配置"},
    )
    events = list(kb_ask.answer_stream("问题"))
    assert events and events[0][0] == "error"
