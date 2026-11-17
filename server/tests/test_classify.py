"""R1：AI 分类的存储语义（不调用模型）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services import kb_classify, kb_store


def _report_row(path: str) -> dict:
    item = kb_store.get_report(path)
    assert item is not None
    return item


def test_classify_stats_and_endpoint_503(client):
    stats = kb_store.classify_stats()
    assert stats["total"] == 3
    assert stats["classified"] == 0
    assert stats["pending"] == 3

    response = client.post("/api/kb/classify", json={"limit": 2})
    assert response.status_code == 503
    assert "AI 未配置" in response.json()["error"]


def test_set_classify_applies_category_and_records_event(client):
    path = "20251231/stock-601800.html"
    result = kb_store.set_classify(
        path,
        {"category": "个股分析", "series": "中国铝业(601800)", "reason": "标题含个股代码"},
        model="test-model",
    )
    assert result == {"path": path, "category": "个股分析", "series": "中国铝业(601800)"}

    item = _report_row(path)
    assert item["category"] == "个股分析"
    assert item["category_ai"] == "个股分析"
    assert item["series"] == "中国铝业(601800)"
    assert item["classify_model"] == "test-model"
    assert item["classify_version"] == kb_store.CLASSIFY_VERSION

    events = kb_store.events_for(path=path, limit=10)
    classify_events = [event for event in events if event["kind"] == "classify"]
    assert len(classify_events) == 1
    payload = classify_events[0]["payload"]
    assert payload["source"] == "ai"
    assert payload["to"]["category"] == "个股分析"
    assert payload["reason"] == "标题含个股代码"

    stats = kb_store.classify_stats()
    assert stats["classified"] == 1
    assert stats["pending"] == 2

    listed = client.get("/api/kb", params={"category": "个股分析"}).json()
    assert [item["path"] for item in listed["items"]] == [path]
    tree = client.get("/api/kb/tree").json()
    assert any(row["name"] == "个股分析" and row["count"] == 1 for row in tree["categories"])


def test_set_classify_rejects_unknown_category(client):
    with pytest.raises(ValueError):
        kb_store.set_classify("20251231/stock-601800.html", {"category": "财经新闻"})


def test_parse_falls_back_to_rule_category():
    item = {
        "path": "20251231/stock-601800.html",
        "category": "个股分析",
        "series": "中国铝业(601800)",
        "category_rule": "个股分析",
        "series_rule": "中国铝业(601800)",
    }
    payload = kb_classify._parse({"category": "财经新闻", "series": "随便", "reason": "x"}, item)
    assert payload["category"] == "个股分析"
    assert payload["fallback"] is True
    assert "越界" in payload["reason"]

    inherited = kb_classify._parse({"category": "个股分析", "series": ""}, item)
    assert inherited["series"] == "中国铝业(601800)"

    changed = kb_classify._parse({"category": "策略研究", "series": ""}, item)
    assert changed["series"] == ""


def test_body_change_resets_ai_classification(client):
    path = "20251231/stock-601800.html"
    kb_store.set_classify(path, {"category": "个股分析", "series": "中国铝业(601800)"})
    assert _report_row(path)["category_ai"] == "个股分析"

    reports_dir = Path(os.environ["KB_REPORTS_DIR"])
    target = reports_dir / path
    html = target.read_text(encoding="utf-8")
    target.write_text(html + "\n<!-- changed -->\n", encoding="utf-8")

    kb_store.sync(force=True)
    item = _report_row(path)
    assert item["category_ai"] == ""
    assert item["classify_version"] == 0
    assert item["category"] == item["category_rule"]


def test_sync_keeps_ai_classification(client):
    """回归：同步不能把「生效分类 ≠ 规则分类」误判成文件变化而清空 AI 分类。"""
    path = "20251231/stock-601800.html"
    kb_store.set_classify(path, {"category": "个股分析", "series": "中国铝业(601800)"})
    kb_store.sync(force=True)
    item = _report_row(path)
    assert item["category_ai"] == "个股分析"
    assert item["category"] == "个股分析"
    assert item["classify_version"] == 1
    assert kb_store.classify_stats()["classified"] == 1


def test_rule_change_keeps_ai_classification(client, monkeypatch):
    """规则基线变化（代码升级/覆盖注释）时只更新 rules，AI 结论与生效值保留。"""
    from app.services import kb_index

    path = "20251231/stock-601800.html"
    kb_store.set_classify(path, {"category": "个股分析", "series": "中国铝业(601800)"})

    original_items = kb_index.cached_scan(force=True)
    mutated = []
    for item in original_items:
        row = dict(item)
        if row["path"] == path:
            row["category"] = "策略研究"
            row["series"] = "量化策略"
        mutated.append(row)
    monkeypatch.setattr(kb_index, "cached_scan", lambda force=False: mutated)

    kb_store.sync(force=True)
    item = _report_row(path)
    assert item["category_rule"] == "策略研究"
    assert item["series_rule"] == "量化策略"
    assert item["category_ai"] == "个股分析"
    assert item["category"] == "个股分析"
    assert item["classify_version"] == 1
