"""把 reports/ 里剩下的附件迁进内容寻址存储（v1.0）。

做三件事（幂等）：
1. 每个非 HTML 文件 → `kb_blob.put()`（本地加密 + COS 加密镜像）；
2. `asset` 表登记「原路径 → sha256」，路由按原路径直接输出（HTML 里的引用不用改）；
3. 删掉原文件，再自底向上清空目录（`reports/` 根本身保留，扫描器需要它存在）。

用法：
    python -m app.cli_migrate_assets            # 预演
    python -m app.cli_migrate_assets --apply    # 真跑
"""

from __future__ import annotations

import json
import mimetypes
import sys
import time
from pathlib import Path

from app import config
from app.services import kb_blob, kb_extract, kb_store

HTML_SUFFIXES = (".html", ".htm")


def _mime_for(rel: str, data: bytes) -> str:
    mime = kb_extract.detect_mime(rel, data) or ""
    if not mime or mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(rel)
        mime = guessed or "application/octet-stream"
    return mime


def _prune_empty_dirs(root: Path) -> int:
    removed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def migrate(apply: bool = False, prune: bool = True) -> dict:
    root = Path(config.REPORTS_DIR)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    plan = []
    skipped_html = 0
    for path in files:
        if path.suffix.lower() in HTML_SUFFIXES:
            skipped_html += 1
            continue
        plan.append((path.relative_to(root).as_posix(), path))

    result = {
        "files": len(files),
        "planned": len(plan),
        "skipped_html": skipped_html,
        "apply": apply,
        "moved": 0,
        "bytes": 0,
        "dirs_removed": 0,
        "errors": [],
    }
    if not apply:
        result["sample"] = [rel for rel, _ in plan[:5]]
        return result

    started = time.time()
    for rel, path in plan:
        try:
            data = path.read_bytes()
        except OSError as exc:
            result["errors"].append(f"{rel}: {exc}")
            continue
        mime = _mime_for(rel, data)
        blob = kb_blob.put(data, mime, rel)
        kb_store.put_asset(rel, blob["sha"], mime, len(data))
        try:
            path.unlink()
        except OSError as exc:
            result["errors"].append(f"{rel}: unlink {exc}")
            continue
        result["moved"] += 1
        result["bytes"] += len(data)

    if prune:
        result["dirs_removed"] = _prune_empty_dirs(root)
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    print(json.dumps(migrate(apply=apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
