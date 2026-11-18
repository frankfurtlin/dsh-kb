"""命令行生成 LLM Wiki 文章：给 Hermes / 运维直接调用（不依赖 DSH 登录）。

用法：
    python -m app.cli_wiki --stats            # 文章进度与候选
    python -m app.cli_wiki --limit 2          # 补写最多 2 篇（hash 命中跳过）
    python -m app.cli_wiki --slug topic-a股市场全景 --force   # 重写指定文章
    python -m app.cli_wiki --list             # 列出候选（按覆盖篇数倒序）
"""

from __future__ import annotations

import argparse
import json
import sys

from app.services import kb_ai, kb_store, kb_wiki


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库 LLM Wiki 文章生成（DSH 默认模型）")
    parser.add_argument("--limit", type=int, default=1, help="本批生成篇数")
    parser.add_argument("--slug", type=str, default="", help="只生成/重写这一篇")
    parser.add_argument("--force", action="store_true", help="hash 命中时也重写")
    parser.add_argument("--stats", action="store_true", help="只打印文章进度")
    parser.add_argument("--list", action="store_true", help="列出候选")
    args = parser.parse_args(argv)

    if args.stats:
        print(json.dumps({"stats": kb_wiki.stats(), "model": kb_ai.model_label()}, ensure_ascii=False))
        return 0
    if args.list:
        print(json.dumps({"candidates": kb_wiki.candidates(limit=30)}, ensure_ascii=False))
        return 0
    if not kb_ai.is_configured():
        print(json.dumps({"ok": False, "error": "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）"}, ensure_ascii=False))
        return 2

    kb_store.sync()
    try:
        if args.slug:
            result = kb_wiki.generate(args.slug.strip(), force=args.force)
        else:
            result = kb_wiki.build_pending(limit=max(1, args.limit), force=args.force)
    except kb_wiki.WikiError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
