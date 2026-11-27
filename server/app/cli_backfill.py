"""回填存量报告的发布时间：python -m app.cli_backfill

v1.0 P0：日期不再是「目录名」，而是 `published_at`（文件名/标题优先，
目录只做兜底）。本脚本把已有行重算一遍，幂等。
"""

from __future__ import annotations

import json

from app.services import kb_store


def main() -> None:
    print(json.dumps(kb_store.backfill_published(), ensure_ascii=False))


if __name__ == "__main__":
    main()
