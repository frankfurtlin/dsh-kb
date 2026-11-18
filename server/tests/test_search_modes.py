"""P0-1：搜索可信度回归。

覆盖三件事：
1. hybrid 模式必须词法命中（垃圾词不能返回全库）；
2. semantic 模式允许 0 条，且去中心余弦低于阈值时不召回；
3. 多关键词按 AND 组合，词法打分能区分整串/分词命中。
"""

from __future__ import annotations

import numpy as np

from app import config
from app.services import kb_embed, kb_store

_DIM = 512


def _seed_vectors(client) -> list[str]:
    """给夹具里的 3 篇报告各写一个正交单位向量（无需真实模型）。"""
    client.get("/api/kb?refresh=1")
    items = client.get("/api/kb", params={"limit": 50}).json()["items"]
    model = config.EMBED_MODEL
    for index, item in enumerate(items):
        vector = np.zeros(_DIM, dtype=np.float32)
        vector[index % _DIM] = 1.0
        kb_store.set_embedding(item["path"], vector, model)
    return [item["path"] for item in items]


def _patch_embed(monkeypatch, query_vector: np.ndarray) -> None:
    monkeypatch.setattr(kb_embed, "available", lambda: True)
    monkeypatch.setattr(kb_embed, "embed_query", lambda q: query_vector)


def test_hybrid_requires_lexical_hit(client, monkeypatch):
    _seed_vectors(client)
    _patch_embed(monkeypatch, np.zeros(_DIM, dtype=np.float32))  # 语义分恒 0

    hit = client.get("/api/kb/search", params={"q": "铝业", "mode": "hybrid"}).json()
    assert hit["mode"] == "hybrid"
    assert hit["total"] == 1, hit
    assert "中国铝业" in hit["items"][0]["title"]

    miss = client.get("/api/kb/search", params={"q": "zzz-no-such-keyword", "mode": "hybrid"}).json()
    assert miss["mode"] == "hybrid"
    assert miss["total"] == 0, miss


def test_semantic_mode_allows_empty_and_respects_cutoff(client, monkeypatch):
    _seed_vectors(client)
    _patch_embed(monkeypatch, np.zeros(_DIM, dtype=np.float32))

    miss = client.get("/api/kb/search", params={"q": "zzz-no-such-keyword", "mode": "semantic"}).json()
    assert miss["mode"] == "semantic"
    assert miss["total"] == 0, miss


def test_multi_keyword_is_and(client, monkeypatch):
    _seed_vectors(client)
    _patch_embed(monkeypatch, np.zeros(_DIM, dtype=np.float32))

    both = client.get("/api/kb/search", params={"q": "铝业 601600", "mode": "hybrid"}).json()
    assert both["total"] == 1, both
    none = client.get("/api/kb/search", params={"q": "铝业 不存在这个词", "mode": "hybrid"}).json()
    assert none["total"] == 0, none


def test_lexical_score_orders_exact_over_tokens():
    exact, match = kb_store._lexical_score("中国铝业(601600) 投资分析报告", "", "中国铝业")
    partial, _ = kb_store._lexical_score("电解铝与有色金属行业周报", "", "中国铝业")
    assert exact == 1.0 and match == "title"
    assert partial == 0.0
    multi, _ = kb_store._lexical_score("中国铝业 601600 分析", "", "中国铝业 601600")
    assert 0 < multi <= 1.0


def test_centered_cosine_suppresses_common_offset():
    matrix = np.array([[1.0, 0.0], [0.9, 0.1]], dtype=np.float32)
    center = matrix.mean(axis=0, keepdims=True)
    cosine = kb_store._centered_cosine(matrix, np.array([1.0, 0.0]), center)
    assert cosine[0] > 0.9
    assert cosine[1] <= cosine[0]
