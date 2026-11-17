"""命令行分类器：给 Hermes 在写完报告后直接调用（不依赖 DSH 登录）。

用法：
    python -m app.cli_classify --limit 20
    python -m app.cli_classify --path 20261113/a-share-market-overview-20261113.html --force
    python -m app.cli_classify --stats
"""

from __future__ import annotations

import argparse
import json
import sys

from app.services import kb_ai, kb_classify, kb_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库 AI 分类（DSH 默认模型）")
    parser.add_argument("--limit", type=int, default=5, help="本批处理篇数")
    parser.add_argument("--path", type=str, default="", help="只处理这一篇（reports/ 相对路径）")
    parser.add_argument("--force", action="store_true", help="已分类的也重跑")
    parser.add_argument("--retry-failed", action="store_true", help="包含上次失败的")
    parser.add_argument("--stats", action="store_true", help="只打印分类进度")
    args = parser.parse_args(argv)

    if args.stats:
        print(json.dumps({"stats": kb_store.classify_stats(), "model": kb_ai.model_label()}, ensure_ascii=False))
        return 0

    if not kb_ai.is_configured():
        print(json.dumps({"ok": False, "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）"}, ensure_ascii=False))
        return 2

    kb_store.sync()
    if args.path:
        path = args.path.strip().lstrip("/")
        if ".." in path.split("/") or not path.lower().endswith((".html", ".htm")):
            print(json.dumps({"ok": False, "error": "无效的报告路径"}, ensure_ascii=False))
            return 1
        result = kb_classify.classify_report(path, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    result = kb_classify.classify_pending(
        limit=max(1, args.limit),
        include_failed=args.retry_failed or args.force,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
