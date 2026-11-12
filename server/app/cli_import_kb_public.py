"""把老站 betterlife 的公开状态导入 dsh-kb 索引库（一次性迁移工具）。

用法（在 server/ 目录下）：

    ../.venv/bin/python -m app.cli_import_kb_public
    ../.venv/bin/python -m app.cli_import_kb_public --file /path/to/kb_public.json --dry-run

只导入 public=true 且报告仍存在的条目，可重复执行（幂等）。
"""

from __future__ import annotations

import argparse
import json
import sys

from app import config
from app.services import kb_public, kb_store

DEFAULT_FILE = "/root/workspace/betterlife/data/kb_public.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入老站公开状态到 dsh-kb")
    parser.add_argument("--file", default=DEFAULT_FILE, help="老站 kb_public.json 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args(argv)

    kb_store.sync()
    if args.dry_run:
        # dry-run：不写库，只核对存在性
        import json as _json
        from pathlib import Path

        source = Path(args.file)
        payload = {"file": str(source), "exists": source.is_file(), "imported": 0, "missing": 0}
        if source.is_file():
            data = _json.loads(source.read_text(encoding="utf-8"))
            for rel, meta in (data.get("items") or {}).items():
                if isinstance(meta, dict) and meta.get("public"):
                    if kb_public.report_exists(kb_public.normalize_rel(rel)):
                        payload["imported"] += 1
                    else:
                        payload["missing"] += 1
        print(_json.dumps(payload, ensure_ascii=False))
        return 0

    result = kb_public.import_legacy_state(args.file)
    result["public_paths"] = kb_store.public_paths()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if "error" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
