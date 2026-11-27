"""把存量文件型报告迁成 DB 型文档（v1.0 P3）。

做三件事（幂等，可重复跑）：
1. 拷一份 HTML 到私有 blob；
2. 报告行原地升级：doc_kind='doc' + uid/blob_sha/mime/source='legacy-import'；
3. 删掉磁盘上的 HTML（`reports/` 里的 assets/ 与 images/ 保留）。

私有报告会把相对素材改写成 `/kb/reports/...` 绝对路径（离开原目录在 blob 里也能显示）；
公开报告保持原文不改写——它继续按原 URL 在 `/kb-public/reports/<path>` 输出，相对素材
照旧解析，公开状态（public=1）随行保留。
用法：
    python -m app.cli_migrate_reports            # 预演（只打印）
    python -m app.cli_migrate_reports --apply    # 真跑
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from app import config
from app.services import kb_blob, kb_store

_REL_ASSET = re.compile(r'(?P<attr>src|href)="(?P<up>(?:\.\./)*)(?P<kind>assets|static|images)/(?P<rest>[^"]*)"')


def rewrite_html(html: str, rel: str) -> str:
    """把相对资源路径改成绝对站内路径，让 blob 里的 HTML 脱离原目录也能显示。"""
    depth = len(str(rel).split("/")) - 1

    def _sub(match: re.Match) -> str:
        attr, kind, rest = match.group("attr"), match.group("kind"), match.group("rest")
        if kind == "assets":
            return f'{attr}="/kb/reports/assets/{rest}"'
        if kind == "static":
            return f'{attr}="/kb/static/{rest}"'
        parent = "/".join(str(rel).split("/")[:-1])
        return f'{attr}="/kb/reports/{parent}/images/{rest}"'

    return _REL_ASSET.sub(_sub, html)


def migrate(apply: bool = False) -> dict:
    reports_root = Path(config.REPORTS_DIR)
    conn = kb_store._connect()
    try:
        rows = conn.execute(
            "SELECT id, path, public FROM reports WHERE COALESCE(doc_kind,'file')='file' ORDER BY path"
        ).fetchall()
    finally:
        conn.close()

    plan, missing = [], 0
    for row in rows:
        rel = row["path"]
        path = reports_root / rel
        if not path.is_file():
            missing += 1
            continue
        plan.append((row["id"], rel, path, bool(row["public"])))

    result = {"rows": len(rows), "planned": len(plan), "missing": missing,
              "public": sum(1 for *_, pub in plan if pub), "migrated": 0,
              "bytes": 0, "apply": apply}
    if not apply:
        result["sample"] = [rel for _, rel, _, _ in plan[:5]]
        return result

    # 每篇单独开短事务：blob.put 内部也会写库，长事务会自锁。
    for report_id, rel, path, is_public in plan:
        raw = path.read_text(encoding="utf-8", errors="replace")
        # 公开报告按原 URL 输出，保持相对素材不变；私有报告改写成绝对路径。
        html = raw if is_public else rewrite_html(raw, rel)
        data = html.encode("utf-8")
        blob = kb_blob.put(data, "text/html", rel.rsplit("/", 1)[-1])
        uid = kb_store._new_uid()
        now = kb_store._now()
        conn = kb_store._connect()
        try:
            conn.execute(
                "UPDATE reports SET doc_kind='doc', uid=?, blob_sha=?, thumb_sha='',"
                " mime='text/html', source='legacy-import', storage_tier=?, size=?, updated_at=?"
                " WHERE id=?",
                (uid, blob["sha"], blob["tier"], len(data), now, report_id),
            )
            kb_store._record_event(conn, rel, "migrate", {
                "uid": uid, "blob": blob["sha"], "bytes": len(data),
                "from": "file", "public": is_public,
            })
            conn.commit()
        finally:
            conn.close()
        path.unlink(missing_ok=True)
        result["migrated"] += 1
        result["bytes"] += len(data)
    return result


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    started = time.time()
    print(json.dumps(migrate(apply=apply), ensure_ascii=False))
    print(f"done in {time.time() - started:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
