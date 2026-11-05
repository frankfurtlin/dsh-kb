"""命令行入口：补 AI sidecar（也可由 Hermes 在一次报告生成后直接调用）。

用法（在 server/ 目录下）：

    python -m app.cli_enrich --stats
    python -m app.cli_enrich --limit 20
    python -m app.cli_enrich --path 20261104/xxx.html --force
    python -m app.cli_enrich --limit 20 --retry-failed
"""

from __future__ import annotations

import argparse
import json
import sys

from app.services import kb_ai, kb_enrich, kb_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-kb AI sidecar 抽取工具")
    parser.add_argument("--limit", type=int, default=0, help="本批最多处理多少篇（0 = 只统计）")
    parser.add_argument("--path", default="", help="只处理指定报告（相对 reports/ 的路径）")
    parser.add_argument("--force", action="store_true", help="即使已有 AI 结果也重新抽取")
    parser.add_argument("--retry-failed", action="store_true", help="同时重试此前失败的报告")
    parser.add_argument("--stats", action="store_true", help="只输出统计")
    args = parser.parse_args(argv)

    if not kb_ai.is_configured() and not args.stats:
        print(json.dumps({"ok": False, "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）"}, ensure_ascii=False))
        return 2

    kb_store.sync()

    if args.stats:
        print(json.dumps(kb_store.ai_stats(), ensure_ascii=False))
        return 0

    if args.path:
        result = kb_enrich.enrich_report(args.path.strip().lstrip("/"), force=args.force)
    else:
        limit = args.limit or 1
        result = kb_enrich.enrich_pending(limit=limit, include_failed=args.retry_failed or args.force)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
