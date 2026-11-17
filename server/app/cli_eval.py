"""检索评测：对 queries.json 跑 lexical / hybrid / hybrid+PPR 三种模式，输出 Recall@5 / MRR@10。

用法：
    python -m app.cli_eval [--limit 10] [--json out.json] [--ppr-weights 0.05,0.1,0.15]

评测是离线的（本地 embedding + SQLite），不调模型、不写库；用于回答
「PPR 第三路召回到底有没有用」这类问题，而不是靠感觉。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from app import config
from app.services import kb_embed, kb_store

_QUERIES = Path(__file__).resolve().parent.parent.parent / "verification" / "eval" / "queries.json"


def _load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["queries"]


def _rank(items: list[dict], gold: str) -> int | None:
    pattern = re.compile(gold)
    for index, item in enumerate(items, 1):
        haystack = f"{item.get('title') or ''} {item.get('path') or ''}"
        if pattern.search(haystack):
            return index
    return None


def _search(mode: str, query: str, limit: int, ppr_weight: float):
    if mode == "lexical":
        # 词法基线：把 query 拆成词，逐词 LIKE 后合并（短语 LIKE 在中文里几乎必然 0 命中，
        # 这样拆词才是对「关键词检索」公平的对照）。
        tokens = [token for token in re.split(r"[\s+，,、/]+", query) if token]
        merged: list[dict] = []
        seen: set[str] = set()
        for token in tokens or [query]:
            items, _ = kb_store.list_reports(q=token, sort="date_desc", offset=0, limit=limit)
            for item in items:
                if item["path"] in seen:
                    continue
                seen.add(item["path"])
                merged.append(item)
        return merged[:limit]
    vector = kb_embed.embed_query(query)
    items, _ = kb_store.hybrid_search(
        query,
        query_vector=vector,
        model=config.EMBED_MODEL,
        offset=0,
        limit=limit,
        ppr_weight=ppr_weight,
    )
    return items


def run(limit: int, ppr_weights: list[float], cases: list[dict], out: Path | None) -> dict:
    if not kb_embed.available():
        raise SystemExit(f"embedding 不可用：{kb_embed.status().get('error')}")
    modes = [("lexical", 0.0), ("hybrid", 0.0)] + [(f"hybrid+ppr[{weight}]", weight) for weight in ppr_weights]
    report: dict = {"limit": limit, "cases": len(cases), "modes": {}}
    started = time.time()
    for label, weight in modes:
        hits = mrr = 0.0
        per_query = []
        mode_started = time.time()
        for case in cases:
            items = _search("lexical" if label == "lexical" else "hybrid", case["query"], limit, weight)
            rank = _rank(items, case["gold"])
            if rank is not None:
                if rank <= 5:
                    hits += 1
                mrr += 1.0 / rank
            per_query.append({"id": case["id"], "query": case["query"], "rank": rank})
        report["modes"][label] = {
            "recall@5": round(hits / len(cases), 4),
            "mrr@10": round(mrr / len(cases), 4),
            "elapsed_s": round(time.time() - mode_started, 2),
            "per_query": per_query,
        }
    report["elapsed_s"] = round(time.time() - started, 2)
    if out:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--ppr-weights", default="", help="逗号分隔，默认用 config.HYBRID_WEIGHT_PPR")
    args = parser.parse_args()
    weights = [float(value) for value in args.ppr_weights.split(",") if value.strip()] or [config.HYBRID_WEIGHT_PPR]
    cases = _load_cases(_QUERIES)
    report = run(args.limit, weights, cases, args.json)
    print(f"评测集：{report['cases']} 条 query，limit={report['limit']}")
    for label, metrics in report["modes"].items():
        print(f"  {label:<20} Recall@5={metrics['recall@5']:.3f}  MRR@10={metrics['mrr@10']:.3f}  ({metrics['elapsed_s']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
